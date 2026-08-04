# -*- coding: utf-8 -*-
"""Точечное наблюдение за состоянием отдельных клавиш (EPIC-13, TASK-336).

Компонент нужен там, где системная регистрация комбинации (RegisterHotKey)
принципиально не подходит:

* US-065 — отмена диктовки клавишей Escape. Регистрировать Escape как
  системную горячую клавишу нельзя: Windows отдавала бы её только нам, и в
  активной программе перестали бы закрываться диалоги и подсказки.
* US-066 — Push-to-Talk. RegisterHotKey сообщает только о срабатывании
  комбинации и не даёт события отпускания вовсе.

Принципы, из которых собран компонент (US-068):

* опрашивается ТОЛЬКО явно переданный белый список виртуальных кодов —
  ни одного «постороннего» кода, никакого чтения символов и содержимого ввода;
* наблюдение живёт ровно столько, сколько длится операция: `start()` при её
  начале, `stop()` во всех точках выхода;
* низкоуровневый клавиатурный хук не ставится и события не поглощаются —
  нажатие как обычно доходит до активной программы;
* состояние клавиш читается у операционной системы (`GetAsyncKeyState`), а не
  накапливается приложением. Поэтому после блокировки и разблокировки Windows
  «залипшему» состоянию просто неоткуда взяться — это ключевое свойство эпика.

Единственная точка обращения к Win32 вынесена в `read_key_state`, поэтому в
тестах состояние клавиш подменяется обычной функцией.
"""
from __future__ import annotations

import ctypes
import logging
import sys
from collections.abc import Callable, Iterable, Sequence

from PySide6.QtCore import QObject, QTimer

log = logging.getLogger(__name__)

# Биты результата GetAsyncKeyState.
KEY_DOWN_MASK = 0x8000  # клавиша нажата прямо сейчас
KEY_PRESSED_SINCE_MASK = 0x0001  # нажималась после предыдущего чтения

_user32 = None
_user32_loaded = False


def _get_async_key_state():
    """Вернуть GetAsyncKeyState или None (не Windows / user32 недоступна)."""
    global _user32, _user32_loaded
    if not _user32_loaded:
        _user32_loaded = True
        if sys.platform == "win32":
            try:
                lib = ctypes.WinDLL("user32", use_last_error=True)
                lib.GetAsyncKeyState.argtypes = [ctypes.c_int]
                lib.GetAsyncKeyState.restype = ctypes.c_short
                _user32 = lib
            except Exception:  # noqa: BLE001
                log.exception("Не удалось загрузить user32 для чтения состояния клавиш")
                _user32 = None
    if _user32 is None:
        return None
    return _user32.GetAsyncKeyState


def read_key_state(virtual_key: int) -> int:
    """Сырое состояние одной клавиши по её виртуальному коду.

    Используется именно GetAsyncKeyState, а не GetKeyState: второй отражает
    состояние очереди сообщений нашего потока и ничего не знает о клавишах,
    нажатых, пока активна чужая программа.

    ВАЖНО: младший бит («нажималась после предыдущего чтения») чтением
    сбрасывается, поэтому за один опрос состояние клавиши читается РОВНО ОДИН
    раз, а оба бита разбираются из полученного значения.
    """
    fn = _get_async_key_state()
    if fn is None:
        return 0
    try:
        return int(fn(int(virtual_key))) & 0xFFFF
    except Exception:  # noqa: BLE001
        return 0


def is_key_pressed(virtual_key: int) -> bool:
    """True, если клавиша нажата физически сейчас."""
    return bool(read_key_state(virtual_key) & KEY_DOWN_MASK)


def _normalize_groups(groups: Iterable[object]) -> tuple[tuple[int, ...], ...]:
    """Привести список кодов к группам «нажата любая из».

    Группа нужна там, где одну логическую клавишу представляют несколько
    виртуальных кодов: Win — это VK_LWIN и VK_RWIN, отдельного «общего» кода у
    неё нет. Без группировки удержание левого Win читалось бы как отпускание
    правого.
    """
    normalized: list[tuple[int, ...]] = []
    for group in groups:
        if isinstance(group, (tuple, list, set, frozenset)):
            codes = tuple(int(code) for code in group)
        else:
            codes = (int(group),)  # type: ignore[arg-type]
        if codes:
            normalized.append(codes)
    return tuple(normalized)


class KeyStateWatcher(QObject):
    """Наблюдение за состоянием заданных клавиш на время одной операции.

    start(groups, on_press=..., on_release=..., ...)
        Начать опрос. `groups` — виртуальные коды; элемент-кортеж означает
        «нажата любая из» (см. `_normalize_groups`). Колбэки получают первый
        код сработавшей группы.
    stop()
        Прекратить опрос и забыть состояние. Вызывать во ВСЕХ точках выхода.

    `assume_pressed=True` заставляет считать все клавиши изначально нажатыми.
    Это режим Push-to-Talk: к моменту доставки WM_HOTKEY через очередь
    сообщений пользователь мог уже отпустить комбинацию, и опрос реального
    состояния зафиксировал бы «всегда отпущено» — то есть отпускания мы бы не
    увидели никогда и запись не остановилась бы. С этим флагом такой случай
    даёт срабатывание на первом же тике.

    `detect_transient_press=True` считает нажатием и то, что успело случиться
    МЕЖДУ тиками (младший бит GetAsyncKeyState). Нужно для отмены по Escape:
    короткий тычок по клавише не должен потеряться из-за интервала опроса.
    """

    DEFAULT_INTERVAL_MS = 40

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        interval_ms: int = DEFAULT_INTERVAL_MS,
        key_state_fn: Callable[[int], int] | None = None,
    ) -> None:
        super().__init__(parent)
        self._read_state = key_state_fn if key_state_fn is not None else read_key_state
        self._timer = QTimer(self)
        self._timer.setInterval(max(10, int(interval_ms)))
        self._timer.timeout.connect(self._poll)
        self._groups: tuple[tuple[int, ...], ...] = ()
        self._pressed: list[bool] = []
        self._on_press: Callable[[int], None] | None = None
        self._on_release: Callable[[int], None] | None = None
        self._detect_transient = False

    @property
    def is_active(self) -> bool:
        return self._timer.isActive()

    @property
    def watched_codes(self) -> tuple[int, ...]:
        """Плоский список опрашиваемых кодов — для тестов и аудита (US-068)."""
        return tuple(code for group in self._groups for code in group)

    def start(
        self,
        groups: Sequence[object],
        *,
        on_press: Callable[[int], None] | None = None,
        on_release: Callable[[int], None] | None = None,
        assume_pressed: bool = False,
        detect_transient_press: bool = True,
    ) -> None:
        self.stop()
        normalized = _normalize_groups(groups)
        if not normalized or (on_press is None and on_release is None):
            return
        self._groups = normalized
        self._on_press = on_press
        self._on_release = on_release
        self._detect_transient = bool(detect_transient_press) and on_press is not None
        # Опрос при старте заодно сбрасывает биты «нажималась ранее», поэтому
        # дальше видно только то, что произошло уже во время наблюдения.
        self._pressed = [
            True if assume_pressed else self._group_pressed(group) for group in normalized
        ]
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        self._groups = ()
        self._pressed = []
        self._on_press = None
        self._on_release = None
        self._detect_transient = False

    def _group_pressed(self, group: tuple[int, ...]) -> bool:
        down = False
        transient = False
        for code in group:
            try:
                state = int(self._read_state(code))
            except Exception:  # noqa: BLE001
                log.exception("Чтение состояния клавиши не удалось: vk=%s", code)
                continue
            if state & KEY_DOWN_MASK:
                down = True
            if state & KEY_PRESSED_SINCE_MASK:
                transient = True
        if down:
            return True
        # Нажатие целиком уместилось между тиками — для отмены по Escape оно
        # так же значимо, как удержание.
        return bool(self._detect_transient and transient)

    def _poll(self) -> None:
        if not self._groups:
            self.stop()
            return
        for index, group in enumerate(self._groups):
            pressed = self._group_pressed(group)
            if pressed == self._pressed[index]:
                continue
            self._pressed[index] = pressed
            callback = self._on_press if pressed else self._on_release
            if callback is None:
                continue
            try:
                callback(group[0])
            except Exception:  # noqa: BLE001
                log.exception("Колбэк наблюдателя клавиш упал: vk=%s", group[0])
            # Типичный колбэк останавливает наблюдение (отмена по Escape, стоп
            # записи в Push-to-Talk). Продолжать обход уже снятого состояния
            # нельзя, поэтому выходим сразу.
            return
