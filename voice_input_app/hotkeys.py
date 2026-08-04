# -*- coding: utf-8 -*-
"""Глобальная горячая клавиша через системную регистрацию Windows (EPIC-13).

Раньше комбинация ловилась низкоуровневым клавиатурным хуком библиотеки
`keyboard`. Отсюда два следствия, ради которых заведён эпик: приложение видело
ВСЕ нажатия в системе (претензия службы информационной безопасности), а после
блокировки и разблокировки Windows карта нажатых клавиш внутри библиотеки
рассинхронизировалась с реальностью — комбинация «залипала» и переставала
работать до перезапуска.

Теперь комбинацию регистрирует сама Windows (`RegisterHotKey`), а приложению
приходит единственное сообщение `WM_HOTKEY` — только о срабатывании ЕГО
комбинации. Сообщение попадает в очередь потока Qt и достаётся из неё
нативным фильтром событий (`QAbstractNativeEventFilter`).

Режимы (US-026 сохранён, US-066 переносит его на новый механизм):

* `toggle` — нажал/нажал ещё раз. Нужно только событие срабатывания, за
  клавишами не следим вовсе.
* `ptt` (Push-to-Talk) — удержание пишет, отпускание останавливает. Событий
  отпускания `RegisterHotKey` не даёт в принципе, поэтому на время записи
  включается точечное наблюдение (`key_watch.KeyStateWatcher`) ровно за теми
  клавишами, из которых состоит комбинация пользователя.

Правило проекта сохранено: новая комбинация регистрируется ДО снятия старой,
и если регистрация провалилась — прежняя остаётся рабочей.

За основу бэкенда (RegisterHotKey + разбор WM_HOTKEY в нативном фильтре) взят
код внешнего контрибьютора из PR #3 kob2kpt-collab/voice-input-local. Отличия
этой реализации: сохранён Push-to-Talk, Escape НЕ регистрируется системной
горячей клавишей (US-065), расширена таблица клавиш и добавлено разрешение
символов нелатинских раскладок (US-064).
"""
from __future__ import annotations

import ctypes
import logging
import re
import sys
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass, field

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication

from .key_watch import KeyStateWatcher

log = logging.getLogger(__name__)

WM_HOTKEY = 0x0312

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
# Без MOD_NOREPEAT удержание комбинации давало бы поток WM_HOTKEY с частотой
# автоповтора клавиатуры. Для Push-to-Talk нужно ровно одно срабатывание.
MOD_NOREPEAT = 0x4000

# Виртуальные коды. Значения — из документации Windows (Virtual-Key Codes).
VK_BACK = 0x08
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt
VK_PAUSE = 0x13
VK_CAPITAL = 0x14
VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_PRIOR = 0x21  # Page Up
VK_NEXT = 0x22  # Page Down
VK_END = 0x23
VK_HOME = 0x24
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_SNAPSHOT = 0x2C  # Print Screen
VK_INSERT = 0x2D
VK_DELETE = 0x2E
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_NUMPAD0 = 0x60
VK_MULTIPLY = 0x6A
VK_ADD = 0x6B
VK_SEPARATOR = 0x6C
VK_SUBTRACT = 0x6D
VK_DECIMAL = 0x6E
VK_DIVIDE = 0x6F
VK_F1 = 0x70
VK_NUMLOCK = 0x90
VK_SCROLL = 0x91

_MODIFIER_CODES = {
    "alt": MOD_ALT,
    "ctrl": MOD_CONTROL,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
}

# Какие физические клавиши опрашивать в Push-to-Talk, чтобы поймать отпускание
# модификатора. У Ctrl/Alt/Shift есть «общий» код (левая и правая клавиши), у
# Win его нет — поэтому пара кодов, и группа считается нажатой, если нажата
# любая из них.
_MODIFIER_WATCH_CODES: dict[str, tuple[int, ...]] = {
    "ctrl": (VK_CONTROL,),
    "alt": (VK_MENU,),
    "shift": (VK_SHIFT,),
    "win": (VK_LWIN, VK_RWIN),
}

# TASK-333: имена клавиш, которые могли попасть в сохранённые настройки
# пользователей (QKeySequence.toString даёт именно такие обозначения).
_NAMED_KEYS: dict[str, int] = {
    "space": VK_SPACE,
    "spacebar": VK_SPACE,
    "enter": VK_RETURN,
    "return": VK_RETURN,
    "tab": VK_TAB,
    "backspace": VK_BACK,
    "back": VK_BACK,
    "insert": VK_INSERT,
    "ins": VK_INSERT,
    "delete": VK_DELETE,
    "del": VK_DELETE,
    "home": VK_HOME,
    "end": VK_END,
    "pageup": VK_PRIOR,
    "pgup": VK_PRIOR,
    "prior": VK_PRIOR,
    "pagedown": VK_NEXT,
    "pgdown": VK_NEXT,
    "pgdn": VK_NEXT,
    "next": VK_NEXT,
    "up": VK_UP,
    "down": VK_DOWN,
    "left": VK_LEFT,
    "right": VK_RIGHT,
    "esc": VK_ESCAPE,
    "escape": VK_ESCAPE,
    "printscreen": VK_SNAPSHOT,
    "printscrn": VK_SNAPSHOT,
    "prtsc": VK_SNAPSHOT,
    "sysreq": VK_SNAPSHOT,
    "pause": VK_PAUSE,
    "scrolllock": VK_SCROLL,
    "capslock": VK_CAPITAL,
    "numlock": VK_NUMLOCK,
    "num*": VK_MULTIPLY,
    "num+": VK_ADD,
    "num-": VK_SUBTRACT,
    "num.": VK_DECIMAL,
    "num/": VK_DIVIDE,
    "multiply": VK_MULTIPLY,
    "add": VK_ADD,
    "subtract": VK_SUBTRACT,
    "decimal": VK_DECIMAL,
    "divide": VK_DIVIDE,
    "separator": VK_SEPARATOR,
}
for _digit in range(10):
    _NAMED_KEYS[f"num{_digit}"] = VK_NUMPAD0 + _digit
    _NAMED_KEYS[f"numpad{_digit}"] = VK_NUMPAD0 + _digit

# OEM-пунктуация. Для латинских символов таблица имеет ПРИОРИТЕТ над VkKeyScan,
# и это осознанно: VkKeyScan отвечает про АКТИВНУЮ раскладку, а в русской, к
# примеру, «;» набирается как Shift+4 — сохранённая комбинация «Ctrl+;»
# превратилась бы в Ctrl и физическую клавишу «4», причём по-разному в
# зависимости от того, какая раскладка была включена при запуске. VkKeyScan
# остаётся для символов, которых в таблице нет, и для нелатинских (ниже).
_ASCII_OEM_KEYS: dict[str, int] = {
    ";": 0xBA,  # VK_OEM_1
    "=": 0xBB,  # VK_OEM_PLUS
    "+": 0xBB,  # та же физическая клавиша: «+» набирается через Shift
    ",": 0xBC,  # VK_OEM_COMMA
    "-": 0xBD,  # VK_OEM_MINUS
    ".": 0xBE,  # VK_OEM_PERIOD
    "/": 0xBF,  # VK_OEM_2
    "`": 0xC0,  # VK_OEM_3
    "[": 0xDB,  # VK_OEM_4
    "\\": 0xDC,  # VK_OEM_5
    "]": 0xDD,  # VK_OEM_6
    "'": 0xDE,  # VK_OEM_7
}

_SUPPORTED_KEYS_HINT = (
    "Поддерживаются буквы, цифры, Space, F1–F24, Enter, Tab, Backspace, Insert, "
    "Delete, Home, End, PgUp, PgDn, стрелки, клавиши numpad и знаки пунктуации."
)


def normalize_hotkey(display: str) -> str:
    text = display.strip().lower()
    text = text.replace("control", "ctrl")
    text = text.replace("windows", "win")
    text = text.replace("meta", "win")
    text = text.replace(" ", "")
    return text


def vk_for_character(char: str) -> int | None:
    """Физический виртуальный код клавиши, которой набирается символ.

    Нужно для нелатинских раскладок (TASK-333): сохранённая комбинация может
    содержать, например, «ctrl+ф», а RegisterHotKey принимает только код
    физической клавиши (у «ф» это код клавиши A). Фиксированная таблица кодов
    тут не годится принципиально — именно на этом спотыкается библиотека
    global-hotkeys (см. спайк TASK-331).

    Сначала пробуем текущую раскладку потока (`VkKeyScanW`), затем все
    установленные в системе (`VkKeyScanExW`): комбинация могла быть задана на
    русской раскладке, а приложение запущено при активной английской.
    Возвращает None, если разрешить символ не удалось или это не Windows.
    """
    if sys.platform != "win32" or not char:
        return None
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.VkKeyScanW.argtypes = [wintypes.WCHAR]
        user32.VkKeyScanW.restype = ctypes.c_short
        result = int(user32.VkKeyScanW(char))
        if result != -1:
            return result & 0xFF

        user32.GetKeyboardLayoutList.argtypes = [ctypes.c_int, ctypes.POINTER(wintypes.HKL)]
        user32.GetKeyboardLayoutList.restype = ctypes.c_int
        user32.VkKeyScanExW.argtypes = [wintypes.WCHAR, wintypes.HKL]
        user32.VkKeyScanExW.restype = ctypes.c_short
        count = int(user32.GetKeyboardLayoutList(0, None))
        if count > 0:
            layouts = (wintypes.HKL * count)()
            user32.GetKeyboardLayoutList(count, layouts)
            for layout in layouts:
                result = int(user32.VkKeyScanExW(char, layout))
                if result != -1:
                    return result & 0xFF
    except Exception:  # noqa: BLE001
        log.exception("Не удалось разрешить символ в виртуальный код: %r", char)
    return None


@dataclass(frozen=True)
class NativeHotkey:
    """Комбинация в терминах Win32: что регистрировать и за чем следить."""

    text: str
    modifiers: int
    virtual_key: int
    # Группы кодов для Push-to-Talk: модификаторы комбинации + основная клавиша.
    # Отпускание любой группы = конец удержания.
    watch_groups: tuple[tuple[int, ...], ...] = field(default=())


def parse_hotkey(display: str, *, vk_resolver: Callable[[str], int | None] | None = None) -> NativeHotkey:
    """Разобрать пользовательскую комбинацию в представление RegisterHotKey.

    Поддерживается ровно одна основная клавиша и минимум один модификатор.
    Требование модификатора — не техническое ограничение: системная
    регистрация одиночной клавиши отобрала бы её у всех остальных программ.
    """
    resolver = vk_resolver if vk_resolver is not None else vk_for_character
    text = normalize_hotkey(display)
    if not text:
        raise ValueError("Горячая клавиша не задана.")
    # Запятая разделяет клавиши в ПОСЛЕДОВАТЕЛЬНОСТИ («Ctrl+K, Ctrl+C»), но она
    # же — обычная клавиша («Ctrl+,»). Различаем по положению: у комбинации с
    # запятой-клавишей она всегда последний символ строки.
    if "," in text[:-1]:
        raise ValueError(
            "Последовательности клавиш не поддерживаются. Задайте одну комбинацию, например Ctrl+Alt+Space."
        )

    # «ctrl++» — это Ctrl и клавиша «+»: разделитель и основная клавиша
    # совпадают, поэтому хвост из двух плюсов разбираем отдельно. Одиночный
    # хвостовой плюс («ctrl+») — это незавершённая комбинация, и она ниже
    # отбрасывается проверкой пустых частей.
    if text.endswith("++"):
        parts = text[:-2].split("+") + ["+"]
    else:
        parts = text.split("+")
    if any(not part for part in parts):
        raise ValueError("Комбинация распознана не полностью. Задайте её заново.")

    modifiers = 0
    canonical_modifiers: list[str] = []
    regular_keys: list[str] = []
    for part in parts:
        if part in _MODIFIER_CODES:
            if part not in canonical_modifiers:
                canonical_modifiers.append(part)
                modifiers |= _MODIFIER_CODES[part]
        else:
            regular_keys.append(part)

    if not modifiers:
        raise ValueError(
            "Добавьте к клавише Ctrl, Alt, Shift или Win. Комбинация без модификатора "
            "была бы отобрана у всех остальных программ: там эта клавиша перестала бы работать."
        )
    if len(regular_keys) != 1:
        raise ValueError("Комбинация должна содержать ровно одну основную клавишу. " + _SUPPORTED_KEYS_HINT)

    key = regular_keys[0]
    virtual_key = _resolve_key(key, resolver)

    ordered = [name for name in ("ctrl", "alt", "shift", "win") if name in canonical_modifiers]
    watch_groups = tuple(_MODIFIER_WATCH_CODES[name] for name in ordered) + ((virtual_key,),)
    canonical = "+".join([*ordered, key])
    return NativeHotkey(canonical, modifiers | MOD_NOREPEAT, virtual_key, watch_groups)


def _resolve_key(key: str, resolver: Callable[[str], int | None]) -> int:
    named = _NAMED_KEYS.get(key)
    if named is not None:
        return named
    if len(key) == 1:
        if "a" <= key <= "z":
            return ord(key.upper())
        if "0" <= key <= "9":
            return ord(key)
        oem = _ASCII_OEM_KEYS.get(key)
        if oem is not None:
            return oem
    match = re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", key)
    if match:
        return VK_F1 + int(match.group(1)) - 1
    if len(key) == 1:
        # Сюда попадают символы нелатинских раскладок («ctrl+ф») и редкая
        # пунктуация: их разрешает Windows, а не таблица (TASK-333).
        resolved = resolver(key)
        if resolved:
            return resolved
    raise ValueError(f"Клавиша «{key}» не поддерживается. " + _SUPPORTED_KEYS_HINT)


class Win32HotkeyBackend:
    """Тонкая обёртка над RegisterHotKey/UnregisterHotKey.

    hwnd=None означает регистрацию на ПОТОК: WM_HOTKEY кладётся в очередь
    сообщений вызывающего потока (у нас — главный поток Qt) и достаётся оттуда
    нативным фильтром событий.
    """

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Системные горячие клавиши поддерживаются только в Windows.")
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        self._user32.RegisterHotKey.restype = wintypes.BOOL
        self._user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        self._user32.UnregisterHotKey.restype = wintypes.BOOL

    def register(self, hotkey_id: int, hotkey: NativeHotkey) -> None:
        ctypes.set_last_error(0)
        if not self._user32.RegisterHotKey(None, hotkey_id, hotkey.modifiers, hotkey.virtual_key):
            error_code = ctypes.get_last_error()
            detail = ctypes.FormatError(error_code).strip() if error_code else "комбинация занята другой программой"
            raise OSError(error_code, detail)

    def unregister(self, hotkey_id: int) -> None:
        if not self._user32.UnregisterHotKey(None, hotkey_id):
            error_code = ctypes.get_last_error()
            if error_code:
                log.warning("UnregisterHotKey failed: id=%s error=%s", hotkey_id, error_code)


class HotkeyService(QAbstractNativeEventFilter):
    """Системная горячая клавиша в двух режимах, без клавиатурного хука."""

    def __init__(
        self,
        on_trigger: Callable[[], None],
        on_press: Callable[[], None] | None = None,
        on_release: Callable[[], None] | None = None,
        *,
        backend: Win32HotkeyBackend | None = None,
        application: QCoreApplication | None = None,
        watcher: KeyStateWatcher | None = None,
    ) -> None:
        super().__init__()
        self.on_trigger = on_trigger
        self.on_press = on_press if on_press is not None else on_trigger
        self.on_release = on_release if on_release is not None else (lambda: None)
        # Совместимость: старый код мог обращаться к self.callback.
        self.callback = on_trigger
        self._backend = backend
        self._application = application
        self._watcher = watcher
        self._filter_installed = False
        self._main_id: int | None = None
        self._next_id = 0x5600
        self._hotkey = ""
        self._mode = "toggle"
        self._parsed: NativeHotkey | None = None
        self._ptt_active = False

    @property
    def hotkey(self) -> str:
        return self._hotkey

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def watched_codes(self) -> tuple[int, ...]:
        """Коды, которые опрашиваются прямо сейчас (пусто вне удержания)."""
        if self._watcher is None:
            return ()
        return self._watcher.watched_codes

    def _ensure_runtime(self) -> None:
        if self._backend is None:
            self._backend = Win32HotkeyBackend()
        if self._application is None:
            self._application = QCoreApplication.instance()
        if self._application is None:
            raise RuntimeError("Qt-приложение ещё не создано.")
        if not self._filter_installed:
            self._application.installNativeEventFilter(self)
            self._filter_installed = True

    def _ensure_watcher(self) -> KeyStateWatcher:
        if self._watcher is None:
            self._watcher = KeyStateWatcher()
        return self._watcher

    def _allocate_id(self) -> int:
        self._next_id += 1
        if self._next_id > 0xBFFF:
            self._next_id = 0x5601
        while self._next_id == self._main_id:
            self._next_id += 1
        return self._next_id

    def start(self, hotkey: str, mode: str = "toggle") -> None:
        new_hotkey = parse_hotkey(hotkey)
        mode = "ptt" if str(mode).lower() == "ptt" else "toggle"
        self._ensure_runtime()

        # Повторная регистрация ТОЙ ЖЕ комбинации новым идентификатором
        # завершилась бы ошибкой «горячая клавиша уже занята» — её занимает наш
        # же предыдущий идентификатор. Поэтому при совпадении меняем только
        # режим: этого достаточно и для defensive-перерегистраций из UI.
        if new_hotkey.text == self._hotkey and self._main_id is not None:
            self._apply_mode(mode)
            self._parsed = new_hotkey
            return

        new_id = self._allocate_id()
        try:
            assert self._backend is not None
            self._backend.register(new_id, new_hotkey)
        except Exception as exc:  # noqa: BLE001
            # Прежняя комбинация не тронута и продолжает работать.
            raise RuntimeError(
                "Не удалось зарегистрировать глобальную горячую клавишу. "
                "Нажмите подсвеченное поле и выберите другую комбинацию, например Ctrl+Alt+Space. "
                f"Детали: {exc}"
            ) from exc

        old_id = self._main_id
        self._main_id = new_id
        self._hotkey = new_hotkey.text
        self._parsed = new_hotkey
        # Комбинация сменилась: наблюдение за клавишами прежней смысла не имеет
        # (оно ждало бы отпускания кодов, которых в новой комбинации нет).
        self._cancel_ptt()
        self._apply_mode(mode)
        if old_id is not None:
            self._backend.unregister(old_id)

    def _apply_mode(self, mode: str) -> None:
        if mode == self._mode:
            return
        self._mode = mode
        # Уходя из Push-to-Talk, гасим наблюдение: в режиме «Переключатель»
        # клавиши не опрашиваются вовсе (US-066, US-068).
        if mode != "ptt":
            self._cancel_ptt()

    def _cancel_ptt(self) -> None:
        self._ptt_active = False
        if self._watcher is not None:
            self._watcher.stop()

    def _begin_ptt(self) -> None:
        if self._ptt_active or self._parsed is None:
            return
        self._ptt_active = True
        watcher = self._ensure_watcher()
        # Наблюдение включается ДО колбэка: старт записи занимает заметное
        # время, и отпускание, случившееся внутри него, не должно потеряться.
        watcher.start(self._parsed.watch_groups, on_release=self._on_ptt_release, assume_pressed=True)
        try:
            self.on_press()
        except Exception:  # noqa: BLE001
            log.exception("Колбэк начала записи (Push-to-Talk) упал")
            self._cancel_ptt()

    def _on_ptt_release(self, _virtual_key: int) -> None:
        if not self._ptt_active:
            return
        self._cancel_ptt()
        try:
            self.on_release()
        except Exception:  # noqa: BLE001
            log.exception("Колбэк остановки записи (Push-to-Talk) упал")

    def _dispatch_hotkey(self, hotkey_id: int) -> None:
        if hotkey_id != self._main_id:
            return
        try:
            if self._mode == "ptt":
                self._begin_ptt()
            else:
                self.on_trigger()
        except Exception:  # noqa: BLE001
            log.exception("Колбэк горячей клавиши упал: id=%s", hotkey_id)

    def nativeEventFilter(self, event_type, message):  # noqa: ANN001, N802
        if event_type in (b"windows_dispatcher_MSG", b"windows_generic_MSG"):
            try:
                msg = wintypes.MSG.from_address(int(message))
                if msg.message == WM_HOTKEY:
                    self._dispatch_hotkey(int(msg.wParam))
            except (TypeError, ValueError):
                log.exception("Не удалось разобрать нативное сообщение Windows")
        # False — сообщение продолжает обычный путь обработки в Qt.
        return False

    def stop(self) -> None:
        self._cancel_ptt()
        if self._main_id is not None and self._backend is not None:
            self._backend.unregister(self._main_id)
        self._main_id = None
        self._hotkey = ""
        self._parsed = None
        if self._filter_installed and self._application is not None:
            self._application.removeNativeEventFilter(self)
            self._filter_installed = False
