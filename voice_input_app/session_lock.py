# -*- coding: utf-8 -*-
"""Определение блокировки экрана на время диктовки (US-069).

Зачем: диктовка, начатая не удержанием клавиши (двойной клик по плашке, кнопка
окна, трей, горячая клавиша в режиме «Переключатель»), продолжала писать в
микрофон после блокировки экрана — пользователь ушёл, а запись идёт. В
Push-to-Talk этого не происходило только потому, что клавиши физически
отпускаются, и наблюдение за ними останавливало запись.

Как определяем. Спрашиваем у Windows состояние САМОЙ СЕССИИ
(`WTSQuerySessionInformation` → `WTSINFOEX.SessionFlags`), а не то, какой
рабочий стол сейчас принимает ввод. Классический приём с `OpenInputDesktop`
отвергнут осознанно: он даёт «заблокировано» на любом защищённом рабочем
столе, в том числе при запросе прав администратора (UAC), и диктовка
обрывалась бы на ровном месте.

Когда спрашиваем. Только пока идёт запись — та же линия, что у отмены по
Escape и Push-to-Talk (EPIC-13): в покое приложение у системы ничего не
спрашивает. Подписка на события сессии (`WTSRegisterSessionNotification`) не
используется: она живёт всё время работы приложения и привязана к окну,
которое прячется в трей.

Сбой любого системного вызова трактуется как «не заблокировано» — из-за
ошибки API диктовка обрываться не должна.
"""
from __future__ import annotations

import ctypes
import logging
import sys
from collections.abc import Callable
from ctypes import wintypes

from PySide6.QtCore import QObject, QTimer

log = logging.getLogger(__name__)

WTS_CURRENT_SERVER_HANDLE = 0
WTS_CURRENT_SESSION = 0xFFFFFFFF
WTS_SESSION_INFO_EX = 25  # WTSSessionInfoEx

# WTSINFOEX_LEVEL1_W.SessionFlags
WTS_SESSIONSTATE_LOCK = 0x00000000
WTS_SESSIONSTATE_UNLOCK = 0x00000001

_WINSTATIONNAME_LENGTH = 32
_USERNAME_LENGTH = 20
_DOMAIN_LENGTH = 17


class _WTSINFOEX_LEVEL1_W(ctypes.Structure):
    _fields_ = [
        ("SessionId", wintypes.ULONG),
        ("SessionState", ctypes.c_int),  # WTS_CONNECTSTATE_CLASS
        ("SessionFlags", wintypes.LONG),
        ("WinStationName", wintypes.WCHAR * (_WINSTATIONNAME_LENGTH + 1)),
        ("UserName", wintypes.WCHAR * (_USERNAME_LENGTH + 1)),
        ("DomainName", wintypes.WCHAR * (_DOMAIN_LENGTH + 1)),
        ("LogonTime", wintypes.LARGE_INTEGER),
        ("ConnectTime", wintypes.LARGE_INTEGER),
        ("DisconnectTime", wintypes.LARGE_INTEGER),
        ("LastInputTime", wintypes.LARGE_INTEGER),
        ("CurrentTime", wintypes.LARGE_INTEGER),
        ("IncomingBytes", wintypes.DWORD),
        ("OutgoingBytes", wintypes.DWORD),
        ("IncomingFrames", wintypes.DWORD),
        ("OutgoingFrames", wintypes.DWORD),
        ("IncomingCompressedBytes", wintypes.DWORD),
        ("OutgoingCompressedBytes", wintypes.DWORD),
    ]


class _WTSINFOEX_LEVEL_W(ctypes.Union):
    _fields_ = [("WTSInfoExLevel1", _WTSINFOEX_LEVEL1_W)]


class _WTSINFOEX_W(ctypes.Structure):
    _fields_ = [("Level", wintypes.DWORD), ("Data", _WTSINFOEX_LEVEL_W)]


_wtsapi = None
_wtsapi_loaded = False


def _get_wtsapi():
    """Загрузить wtsapi32 (или вернуть None: не Windows / библиотека недоступна)."""
    global _wtsapi, _wtsapi_loaded
    if not _wtsapi_loaded:
        _wtsapi_loaded = True
        if sys.platform == "win32":
            try:
                lib = ctypes.WinDLL("wtsapi32", use_last_error=True)
                lib.WTSQuerySessionInformationW.argtypes = [
                    wintypes.HANDLE,
                    wintypes.DWORD,
                    ctypes.c_int,
                    ctypes.POINTER(ctypes.c_void_p),
                    ctypes.POINTER(wintypes.DWORD),
                ]
                lib.WTSQuerySessionInformationW.restype = wintypes.BOOL
                lib.WTSFreeMemory.argtypes = [ctypes.c_void_p]
                lib.WTSFreeMemory.restype = None
                _wtsapi = lib
            except Exception:  # noqa: BLE001
                log.exception("Не удалось загрузить wtsapi32 для чтения состояния сессии")
                _wtsapi = None
    return _wtsapi


def is_session_locked() -> bool:
    """True, если сессия пользователя сейчас заблокирована (экран блокировки).

    Любая ошибка — «не заблокировано»: обрывать диктовку из-за сбоя обращения
    к системе нельзя.
    """
    lib = _get_wtsapi()
    if lib is None:
        return False
    buffer = ctypes.c_void_p()
    size = wintypes.DWORD(0)
    try:
        ok = lib.WTSQuerySessionInformationW(
            WTS_CURRENT_SERVER_HANDLE,
            WTS_CURRENT_SESSION,
            WTS_SESSION_INFO_EX,
            ctypes.byref(buffer),
            ctypes.byref(size),
        )
        if not ok or not buffer or size.value < ctypes.sizeof(_WTSINFOEX_W):
            return False
        info = ctypes.cast(buffer, ctypes.POINTER(_WTSINFOEX_W)).contents
        if info.Level != 1:
            return False
        return int(info.Data.WTSInfoExLevel1.SessionFlags) == WTS_SESSIONSTATE_LOCK
    except Exception:  # noqa: BLE001
        log.exception("Не удалось прочитать состояние сессии")
        return False
    finally:
        if buffer:
            try:
                lib.WTSFreeMemory(buffer)
            except Exception:  # noqa: BLE001
                pass


class SessionLockWatcher(QObject):
    """Наблюдение за блокировкой экрана на время одной операции (US-069).

    По устройству и правилам жизни повторяет `key_watch.KeyStateWatcher`:
    `start()` при начале записи, `stop()` во всех точках её завершения, между
    ними — опрос раз в секунду. Колбэк срабатывает ОДИН раз, на переходе
    «не заблокировано → заблокировано»; состояние на старте считывается, чтобы
    уже заблокированный экран не выглядел новым событием.
    """

    DEFAULT_INTERVAL_MS = 1000

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        interval_ms: int = DEFAULT_INTERVAL_MS,
        locked_fn: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self._is_locked = locked_fn if locked_fn is not None else is_session_locked
        self._timer = QTimer(self)
        self._timer.setInterval(max(200, int(interval_ms)))
        self._timer.timeout.connect(self._poll)
        self._locked = False
        self._on_locked: Callable[[], None] | None = None

    @property
    def is_active(self) -> bool:
        return self._timer.isActive()

    def start(self, on_locked: Callable[[], None]) -> None:
        self.stop()
        if on_locked is None:
            return
        self._on_locked = on_locked
        self._locked = self._read_locked()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        self._on_locked = None
        self._locked = False

    def _read_locked(self) -> bool:
        try:
            return bool(self._is_locked())
        except Exception:  # noqa: BLE001
            log.exception("Проверка блокировки экрана не удалась")
            return False

    def _poll(self) -> None:
        callback = self._on_locked
        if callback is None:
            # Наблюдение снято — состояние сессии не спрашиваем вовсе.
            self.stop()
            return
        locked = self._read_locked()
        was_locked = self._locked
        self._locked = locked
        if not locked or was_locked:
            return
        try:
            callback()
        except Exception:  # noqa: BLE001
            log.exception("Колбэк блокировки экрана упал")
