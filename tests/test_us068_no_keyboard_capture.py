# -*- coding: utf-8 -*-
r"""Регресс-тесты US-068: приложение не перехватывает клавиатурный ввод.

Это обещание, которое ИТ и служба информационной безопасности проверяют перед
установкой, поэтому оно закреплено автоматикой, а не только словами в
документации. Тест падает, если кто-то вернёт в код перехват — намеренно или по
привычке подключив библиотеку, которая ставит хук.

Что проверяется:

1. СТАТИЧЕСКИЙ guard (TASK-347): в исходниках нет установки низкоуровневого
   клавиатурного хука (`SetWindowsHookEx`/`WH_KEYBOARD_LL`), нет регистрации
   сырого ввода (`RegisterRawInputDevices`) и нет библиотек `keyboard`/
   `pyautogui` — ни в коде, ни в requirements, ни в сборке.
2. Набор читаемых виртуальных кодов вычисляется из пользовательских настроек
   (комбинация + Escape), а не задан произвольным списком.
3. ПОКОЙ (TASK-348): пока нет записи и распознавания, ни один механизм чтения
   клавиатуры не активен, и все наблюдатели гаснут при выходе из приложения.

Тест headless: Win32 не вызывается. Запуск (в venv приложения):
    .venv\Scripts\python.exe tests\test_us068_no_keyboard_capture.py
"""
from __future__ import annotations

import ast
import os
import re
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["LOCALAPPDATA"] = tempfile.gettempdir()

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from PySide6.QtCore import QCoreApplication  # noqa: E402

from voice_input_app.hotkeys import VK_ESCAPE, parse_hotkey  # noqa: E402
from voice_input_app.key_watch import KeyStateWatcher  # noqa: E402
from voice_input_app.session_lock import SessionLockWatcher  # noqa: E402

_APP = QCoreApplication.instance() or QCoreApplication(sys.argv)

PACKAGE_DIR = REPO_ROOT / "voice_input_app"
UI_PATH = PACKAGE_DIR / "ui.py"
DOC_PATH = REPO_ROOT / "docs" / "enterprise-deploy.md"

# Механизмы, которыми приложение НЕ имеет права пользоваться.
FORBIDDEN_MECHANISMS = (
    "SetWindowsHookEx",      # низкоуровневый хук клавиатуры
    "WH_KEYBOARD_LL",
    "RegisterRawInputDevices",  # сырой ввод
    "GetKeyboardState",      # состояние ВСЕЙ клавиатуры разом
    "GetKeyNameText",
    "ToUnicodeEx",           # расшифровка нажатия в символ
)

FORBIDDEN_LIBRARIES = ("import keyboard", "import pyautogui", "keyboard.hook", "keyboard.add_hotkey", "pyautogui.")


def _functions_touching(source: str, name: str) -> set[str]:
    """Имена функций, в теле которых упоминается `name` (разбор кода, не текста)."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Attribute) and inner.attr == name:
                found.add(node.name)
            elif isinstance(inner, ast.Name) and inner.id == name:
                found.add(node.name)
    return found


def _runtime_sources() -> dict[str, str]:
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(PACKAGE_DIR.glob("*.py"))
    }


def test_no_low_level_keyboard_hook_anywhere() -> None:
    for name, src in _runtime_sources().items():
        for mechanism in FORBIDDEN_MECHANISMS:
            assert mechanism not in src, f"{name}: добавлен перехват клавиатуры — {mechanism}"


def test_no_keyboard_capturing_libraries() -> None:
    for name, src in _runtime_sources().items():
        for marker in FORBIDDEN_LIBRARIES:
            assert marker not in src, f"{name}: вернулась библиотека, ставящая хук — {marker}"

    requirements = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    assert "keyboard>=" not in requirements and "pyautogui" not in requirements

    build = (REPO_ROOT / "build_exe.bat").read_text(encoding="utf-8").lower()
    assert "hidden-import keyboard" not in build and "hidden-import pyautogui" not in build

    spec = REPO_ROOT / "VoiceInputLocal.spec"
    if spec.exists():
        spec_src = spec.read_text(encoding="utf-8").lower()
        assert "'keyboard'" not in spec_src and "'pyautogui'" not in spec_src


def test_only_sanctioned_reading_of_key_state() -> None:
    """Состояние клавиш читается ровно в двух местах — оба перечислимы и объяснимы.

    `key_watch.py` — отмена по Escape и отпускание комбинации в Push-to-Talk,
    только на время операции. `insert.py` — короткая проверка перед вставкой,
    не удерживаются ли модификаторы (иначе Ctrl+V превратился бы в другую
    команду). Больше нигде состояние клавиш не читается.
    """
    sources = _runtime_sources()
    readers = {name for name, src in sources.items() if "GetAsyncKeyState" in src}
    assert readers == {"key_watch.py", "insert.py"}, (
        f"состояние клавиш читается в неожиданном месте: {sorted(readers)}"
    )

    # Внутри каждого файла чтение сосредоточено в перечислимых функциях —
    # проверяем по разбору кода, а не по числу упоминаний в тексте.
    key_watch_src = sources["key_watch.py"]
    assert _functions_touching(key_watch_src, "GetAsyncKeyState") == {
        "_get_async_key_state",  # единственное место, где функция берётся из user32
    }, "в key_watch.py появилось прямое обращение к GetAsyncKeyState"
    assert _functions_touching(key_watch_src, "_get_async_key_state") == {
        "read_key_state",  # единственное место, где состояние клавиши читается
    }, "в key_watch.py появилось новое место чтения клавиш"

    assert _functions_touching(sources["insert.py"], "GetAsyncKeyState") == {
        "__init__",      # объявление сигнатуры ctypes
        "any_pressed",   # проверка перед вставкой по фиксированному списку
    }, "в insert.py появилось новое место чтения клавиш"

    assert "PASTE_GUARD_KEYS" in sources["insert.py"], "проверка перед вставкой читает не фиксированный список"
    assert "watched_codes" in sources["key_watch.py"], "нет способа перечислить опрашиваемые коды"


def test_watched_codes_come_from_user_settings() -> None:
    """Опрашиваются коды выбранной пользователем комбинации, а не произвольные."""
    parsed = parse_hotkey("Ctrl+Alt+Space")
    watched = {code for group in parsed.watch_groups for code in group}
    assert watched == {0x11, 0x12, 0x20}, watched  # Ctrl, Alt, Space — и ничего сверх

    other = parse_hotkey("Shift+Win+D")
    other_watched = {code for group in other.watch_groups for code in group}
    assert 0x10 in other_watched and ord("D") in other_watched
    assert 0x20 not in other_watched, "набор кодов не следует за настройкой пользователя"

    # Отмена по Escape — ровно один код.
    assert VK_ESCAPE == 0x1B


def test_nothing_is_read_while_idle() -> None:
    """TASK-348: в покое ни один механизм чтения не активен."""
    reads: list[int] = []
    key_watcher = KeyStateWatcher(key_state_fn=lambda vk: (reads.append(vk), 0)[1])
    lock_watcher = SessionLockWatcher(locked_fn=lambda: (reads.append(-1), False)[1])

    assert key_watcher.is_active is False
    assert key_watcher.watched_codes == ()
    assert lock_watcher.is_active is False
    assert reads == [], "в покое приложение читает состояние системы"


def test_watchers_stop_and_read_nothing_after_stop() -> None:
    reads: list[int] = []
    watcher = KeyStateWatcher(key_state_fn=lambda vk: (reads.append(vk), 0)[1])
    watcher.start([VK_ESCAPE], on_press=lambda _vk: None)
    assert watcher.watched_codes == (VK_ESCAPE,)

    watcher.stop()
    reads.clear()
    watcher._poll()

    assert watcher.is_active is False
    assert reads == [], "после остановки наблюдение продолжает читать клавиши"


def test_quit_stops_every_watcher() -> None:
    """US-049: при выходе из приложения наблюдатели гасятся явно."""
    src = UI_PATH.read_text(encoding="utf-8")
    start = src.index("    def really_quit(self)")
    end = src.find("\n    def ", start + 10)
    body = src[start: end if end != -1 else len(src)]
    assert "stop_escape_watch()" in body, "really_quit не гасит наблюдение за Escape"
    assert "_stop_session_lock_watch()" in body, "really_quit не гасит наблюдение за блокировкой экрана"
    assert "self.hotkey.stop()" in body, "really_quit не снимает системную регистрацию (и наблюдение PTT)"


def test_toggle_mode_never_watches_keys() -> None:
    """В режиме «Переключатель» клавиши не опрашиваются вовсе."""
    src = (PACKAGE_DIR / "hotkeys.py").read_text(encoding="utf-8")
    start = src.index("    def _dispatch_hotkey(self")
    end = src.find("\n    def ", start + 10)
    body = src[start: end if end != -1 else len(src)]
    assert '_begin_ptt()' in body and 'self._mode == "ptt"' in body, (
        "наблюдение за клавишами больше не привязано к режиму Push-to-Talk"
    )


def test_security_section_exists_in_it_documentation() -> None:
    """TASK-349: ИТ и служба ИБ должны видеть, что именно читается и когда."""
    doc = DOC_PATH.read_text(encoding="utf-8")
    assert "информационной безопасности" in doc, "нет раздела для службы информационной безопасности"
    for expected in ("RegisterHotKey", "GetAsyncKeyState", "Push-to-Talk", "Escape"):
        assert expected in doc, f"в разделе для ИБ не описан механизм: {expected}"
    assert re.search(r"низкоуровнев\w+ (клавиатурн\w+ )?хук", doc), (
        "в документации не сказано прямо, что низкоуровневый хук не ставится"
    )


def _run() -> None:
    tests = [
        test_no_low_level_keyboard_hook_anywhere,
        test_no_keyboard_capturing_libraries,
        test_only_sanctioned_reading_of_key_state,
        test_watched_codes_come_from_user_settings,
        test_nothing_is_read_while_idle,
        test_watchers_stop_and_read_nothing_after_stop,
        test_quit_stops_every_watcher,
        test_toggle_mode_never_watches_keys,
        test_security_section_exists_in_it_documentation,
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print("US-068 regression: ALL PASS")


if __name__ == "__main__":
    _run()
