# -*- coding: utf-8 -*-
"""US-072: стартовая проверка облачных подключений (EPIC-6).

Зачем модуль: при автозапуске вместе с Windows сеть и корпоративный прокси ещё
поднимаются, поэтому ЕДИНСТВЕННАЯ проверка соединения почти всегда падала и
пользователь видел «Неверный API-ключ», хотя с ключом всё в порядке. Здесь
собрано всё, что нужно для честной стартовой проверки:

* лестница повторов RETRY_DELAYS_MS (5, 10 и 20 секунд после первой попытки);
* гейт готовности сети — HTTP-запрос не отправляется, пока TCP до хоста не
  поднимается (это и есть «проверка не выполняется раньше готовности сети»);
* МАШИННЫЙ признак причины сбоя (FAIL_*) вместо разбора русской строки
  verify_* — только по нему решается, писать ли «Неверный API-ключ»;
* сбор проверяемых эндпоинтов из реестра cfg.cloud_connections (а не из
  устаревших полей openai_stt_* / elevenlabs_stt_*).

Модуль намеренно без Qt: состояние лестницы держит MainWindow (там же живут
QTimer и воркер), а здесь — чистые функции, которые легко проверить тестом.

ВАЖНО (контракт US-073): списки моделей здесь только ПОЛУЧАЮТСЯ через
cloud_stt.discover_models / cloud_llm.discover_chat_models. Регистрация в
реестре моделей — исключительно через ModelManager.set_cloud_models на стороне
UI, чтобы обязательный фильтр моделей нельзя было обойти.
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence
from urllib.parse import urlparse

from .logger import get_logger

log = get_logger("cloud_startup")

# ── Лестница повторов ────────────────────────────────────────────────────
# Первую проверку планирует MainWindow сразу после запуска; при неудаче она
# повторяется через 5, 10 и 20 секунд. Сообщение показывается ТОЛЬКО когда
# лестница исчерпана (AC 2) — иначе пользователь видел бы ошибку от первой же
# попытки, сделанной ещё до готовности сети.
RETRY_DELAYS_MS: tuple = (5000, 10000, 20000)

# ── Машинные признаки причины (AC 3) ─────────────────────────────────────
FAIL_NO_NETWORK = "no_network"   # сеть недоступна (нет TCP/DNS до хоста)
FAIL_TIMEOUT = "timeout"         # ответ не пришёл вовремя
FAIL_PROXY = "proxy"             # запрос не пропустил прокси (ProxyError / HTTP 407)
FAIL_TLS = "tls"                 # не установилось защищённое соединение
FAIL_AUTH = "auth"               # сервис ОТВЕТИЛ отказом на запрос с ключом (401/403)
FAIL_HTTP = "http"               # сервис ответил другой ошибкой (5xx, 404, ...)
FAIL_CONFIG = "config"           # реквизиты не заполнены / недопустимый ключ
FAIL_UNKNOWN = "unknown"         # неожиданное исключение

# Единственный признак, дающий право написать «Неверный API-ключ» (AC 3).
AUTH_STATUS_CODES = (401, 403)

ELEVENLABS_PROBE_URL = "https://api.elevenlabs.io/v1/user"

ROLE_DICTATION = "dictation"
ROLE_POSTPROCESS = "postprocess"
ROLE_SUMMARY = "summary"

_ROLE_NAMES = {
    ROLE_DICTATION: "диктовка",
    ROLE_POSTPROCESS: "постобработка",
    ROLE_SUMMARY: "суммаризация",
}


@dataclass
class CheckTarget:
    """Один проверяемый эндпоинт.

    Несколько подключений с одинаковыми реквизитами (тип + адрес + ключ)
    склеиваются в одну цель — двойной проверки одного и того же эндпоинта быть
    не должно, поэтому connection_ids это список, а не одно значение.
    """

    ctype: str = "openai"           # openai | elevenlabs
    base_url: str = ""
    api_key: str = ""
    name: str = ""
    connection_ids: tuple = ()
    roles: tuple = ()

    @property
    def probe_url(self) -> str:
        """Лёгкий GET-эндпоинт для проверки ключа."""
        if self.ctype == "elevenlabs":
            return ELEVENLABS_PROBE_URL
        return (self.base_url or "").rstrip("/") + "/models"

    @property
    def host(self) -> str:
        try:
            return urlparse(self.probe_url).hostname or ""
        except Exception:  # noqa: BLE001
            return ""

    @property
    def title(self) -> str:
        return self.name or self.host or self.ctype


@dataclass
class CheckOutcome:
    """Результат одной попытки по одной цели."""

    target: CheckTarget
    ok: bool = False
    kind: str = ""                  # FAIL_* при ok=False
    status_code: Optional[int] = None
    detail: str = ""                # техническая подробность для журнала
    elapsed: float = 0.0
    stt_models: list = field(default_factory=list)
    chat_models: list = field(default_factory=list)

    @property
    def event(self) -> str:
        """Код ответа или тип сбоя — то, что пишется в журнал (AC 7)."""
        if self.status_code is not None:
            return f"HTTP {self.status_code}"
        return self.kind or ("ok" if self.ok else FAIL_UNKNOWN)


# ── Сбор целей из реестра подключений (AC 6) ─────────────────────────────


def _signature(ctype: str, base_url: str, api_key: str):
    return (ctype, (base_url or "").strip().rstrip("/").lower(), api_key or "")


def build_targets(cfg) -> list:
    """Собрать список эндпоинтов для стартовой проверки.

    Источник правды — реестр cfg.cloud_connections (US-037): проверяются ВСЕ
    подключения с заполненным ключом, а не только те, на которые ссылаются
    устаревшие поля. Подключения с общими реквизитами склеиваются в одну цель.

    Устаревшие поля (openai_stt_* / elevenlabs_stt_* / реквизиты постобработки
    и суммаризации) используются только как запасной источник — когда реестр
    пуст (config.json правился руками). При обычной загрузке config их
    переносит в реестр миграция _migrate_to_connections, поэтому второй
    проверки того же эндпоинта не возникает.
    """
    by_sig = {}
    order = []

    def add(ctype: str, base_url: str, api_key: str, name: str,
            conn_id: str = "", roles: Sequence = ()) -> None:
        if not api_key:
            return
        if ctype == "openai" and not (base_url or "").strip():
            return
        sig = _signature(ctype, base_url, api_key)
        target = by_sig.get(sig)
        if target is None:
            target = CheckTarget(ctype=ctype, base_url=base_url or "", api_key=api_key, name=name or "")
            by_sig[sig] = target
            order.append(sig)
        elif name and name not in target.name:
            target.name = f"{target.name} / {name}" if target.name else name
        if conn_id and conn_id not in target.connection_ids:
            target.connection_ids = target.connection_ids + (conn_id,)
        for role in roles:
            if role not in target.roles:
                target.roles = target.roles + (role,)

    connections = list(getattr(cfg, "cloud_connections", None) or [])
    if connections:
        pp_id = getattr(cfg, "postprocess_connection_id", "") or ""
        sm_id = getattr(cfg, "summary_connection_id", "") or ""
        pp_on = bool(getattr(cfg, "postprocess_enabled", False))
        sm_on = (getattr(cfg, "summary_mode", "local") or "local") == "cloud"
        for conn in connections:
            roles = [ROLE_DICTATION]
            if pp_on and conn.id and conn.id == pp_id:
                roles.append(ROLE_POSTPROCESS)
            if sm_on and conn.id and conn.id == sm_id:
                roles.append(ROLE_SUMMARY)
            add(conn.type, conn.base_url, conn.api_key, conn.name, conn.id, roles)
    else:
        # Запасной источник: реестр пуст, но старые поля заполнены.
        add("openai", getattr(cfg, "openai_stt_base_url", "") or "https://api.openai.com/v1",
            getattr(cfg, "openai_stt_api_key", "") or "", "OpenAI STT", roles=[ROLE_DICTATION])
        add("elevenlabs", "", getattr(cfg, "elevenlabs_stt_api_key", "") or "",
            "ElevenLabs", roles=[ROLE_DICTATION])
        if getattr(cfg, "postprocess_enabled", False):
            add("openai", getattr(cfg, "postprocess_base_url", "") or "https://api.openai.com/v1",
                getattr(cfg, "postprocess_api_key", "") or "", "Постобработка", roles=[ROLE_POSTPROCESS])
        if (getattr(cfg, "summary_mode", "local") or "local") == "cloud":
            add("openai", getattr(cfg, "summary_base_url", "") or "https://api.openai.com/v1",
                getattr(cfg, "summary_api_key", "") or "", "Суммаризация", roles=[ROLE_SUMMARY])

    return [by_sig[sig] for sig in order]


# ── Классификация причины ────────────────────────────────────────────────


def classify_status(status_code: int) -> str:
    """Причина по коду ответа сервиса. 401/403 — единственный путь к FAIL_AUTH."""
    if status_code in AUTH_STATUS_CODES:
        return FAIL_AUTH
    if status_code == 407:
        return FAIL_PROXY
    return FAIL_HTTP


def classify_exception(exc: BaseException) -> str:
    """Причина по типу исключения.

    Разбор идёт по ИМЕНАМ классов в MRO, а не через isinstance(requests.X):
    так классификация не зависит от того, установлен ли requests, и её можно
    проверить тестом с поддельными исключениями.
    """
    names = {cls.__name__ for cls in type(exc).__mro__}
    if "ProxyError" in names:
        return FAIL_PROXY
    if "SSLError" in names:
        return FAIL_TLS
    # ConnectTimeout наследует и Timeout, и ConnectionError — таймаут важнее.
    if names & {"Timeout", "ConnectTimeout", "ReadTimeout", "timeout", "TimeoutError"}:
        return FAIL_TIMEOUT
    if "ConnectionError" in names:
        return FAIL_NO_NETWORK
    if "gaierror" in names:
        return FAIL_NO_NETWORK
    return FAIL_UNKNOWN


def failure_text(outcome) -> str:
    """Человеческая причина сбоя (AC 3).

    «Неверный API-ключ» появляется ТОЛЬКО при FAIL_AUTH, то есть когда сервис
    действительно ответил отказом (HTTP 401/403) на запрос с ключом.
    """
    host = outcome.target.host or outcome.target.probe_url
    kind = outcome.kind
    if kind == FAIL_AUTH:
        return f"Неверный API-ключ — сервис отклонил его (HTTP {outcome.status_code})"
    if kind == FAIL_NO_NETWORK:
        return f"сеть недоступна — нет связи с {host}"
    if kind == FAIL_TIMEOUT:
        return f"ответ от {host} не пришёл вовремя"
    if kind == FAIL_PROXY:
        return f"запрос к {host} не пропустил прокси"
    if kind == FAIL_TLS:
        return f"не удалось установить защищённое соединение с {host}"
    if kind == FAIL_HTTP:
        return f"сервис ответил ошибкой HTTP {outcome.status_code}"
    if kind == FAIL_CONFIG:
        return outcome.detail or "реквизиты подключения не заполнены"
    return f"сбой проверки: {outcome.detail}" if outcome.detail else "сбой проверки"


def summarize_failures(outcomes: Sequence) -> str:
    """Одно сообщение по всем неудачным целям (одно уведомление, а не N)."""
    parts = []
    for outcome in outcomes:
        roles = [_ROLE_NAMES[r] for r in outcome.target.roles if r in _ROLE_NAMES]
        suffix = f" [{', '.join(roles)}]" if roles else ""
        parts.append(f"{outcome.target.title}{suffix}: {failure_text(outcome)}")
    return "; ".join(parts)


def next_retry_delay_ms(attempts_done: int) -> Optional[int]:
    """Пауза перед следующим повтором или None, если лестница исчерпана.

    attempts_done — сколько проверок уже выполнено (первая = 1).
    """
    index = max(0, attempts_done - 1)
    if index >= len(RETRY_DELAYS_MS):
        return None
    return RETRY_DELAYS_MS[index]


def total_attempts() -> int:
    """Всего проверок: первая + повторы по лестнице."""
    return 1 + len(RETRY_DELAYS_MS)


# ── Сама проверка ────────────────────────────────────────────────────────


def _default_network_probe(host: str) -> bool:
    if not host:
        return True
    try:
        from . import cloud_stt
        return cloud_stt.is_internet_available(host)
    except Exception:  # noqa: BLE001
        # Дешёвый запасной путь, если модуль недоступен: обычный TCP-connect.
        try:
            with socket.create_connection((host, 443), timeout=3.0):
                return True
        except OSError:
            return False


def _default_http_get(url: str, headers: dict):
    import requests

    from .cloud_stt import CONNECT_TIMEOUT, VERIFY_READ_TIMEOUT

    return requests.get(url, headers=headers, timeout=(CONNECT_TIMEOUT, VERIFY_READ_TIMEOUT))


def _config_problem(target) -> Optional[str]:
    if not target.api_key:
        return "не заполнен API-ключ"
    if target.ctype != "elevenlabs" and not (target.base_url or "").strip():
        return "не заполнен адрес подключения"
    try:
        from .cloud_stt import _validate_api_key_charset
        return _validate_api_key_charset(target.api_key)
    except Exception:  # noqa: BLE001
        return None


def probe_target(
    target,
    *,
    attempt: int = 1,
    network_probe: Optional[Callable] = None,
    http_get: Optional[Callable] = None,
):
    """Одна попытка по одной цели: гейт сети, GET, классификация.

    HTTP-запрос НЕ отправляется, пока не поднимается TCP до хоста (AC 1) —
    именно из-за этого раньше первая же проверка при загрузке Windows падала
    и выглядела как отказ ключа.
    """
    outcome = CheckOutcome(target=target)
    problem = _config_problem(target)
    if problem:
        outcome.kind = FAIL_CONFIG
        outcome.detail = problem
        _log_attempt(attempt, outcome, "реквизиты не готовы")
        return outcome

    probe = network_probe or _default_network_probe
    started = time.monotonic()
    try:
        network_ready = bool(probe(target.host))
    except Exception as exc:  # noqa: BLE001
        # Сбой самой проверки сети не должен блокировать запрос — идём дальше.
        log.warning("startup check: сбой проверки сети для %s: %s", target.host, exc)
        network_ready = True
    if not network_ready:
        outcome.kind = FAIL_NO_NETWORK
        outcome.detail = "нет TCP-соединения с хостом"
        outcome.elapsed = time.monotonic() - started
        _log_attempt(attempt, outcome, "запрос не отправлялся — сеть ещё не готова")
        return outcome

    getter = http_get or _default_http_get
    headers = ({"xi-api-key": target.api_key} if target.ctype == "elevenlabs"
               else {"Authorization": f"Bearer {target.api_key}"})
    try:
        response = getter(target.probe_url, headers)
    except Exception as exc:  # noqa: BLE001
        outcome.kind = classify_exception(exc)
        outcome.detail = f"{type(exc).__name__}: {exc}"
        outcome.elapsed = time.monotonic() - started
        _log_attempt(attempt, outcome, "попытка не удалась")
        return outcome

    outcome.elapsed = time.monotonic() - started
    outcome.status_code = int(getattr(response, "status_code", 0) or 0)
    if outcome.status_code == 200:
        outcome.ok = True
        _log_attempt(attempt, outcome, "подключение доступно")
        return outcome
    outcome.kind = classify_status(outcome.status_code)
    outcome.detail = str(getattr(response, "text", "") or "")[:200]
    _log_attempt(attempt, outcome, "попытка не удалась")
    return outcome


def discover_for_target(target):
    """Списки моделей после успешной проверки: (STT, chat).

    Chat-модели запрашиваются только если подключение используется
    постобработкой или суммаризацией — лишний HTTP при старте не нужен.
    Функции discover_* вызываются КАК ЕСТЬ (контракт с US-073).
    """
    stt_models = []
    chat_models = []
    try:
        from . import cloud_stt
        cloud_stt.invalidate_discover_cache()
        stt_models = list(cloud_stt.discover_models(
            target.ctype, target.api_key, target.base_url or None) or [])
    except Exception as exc:  # noqa: BLE001
        log.warning("startup check: discover_models(%s) не удался: %s", target.title, exc)
    needs_chat = bool({ROLE_POSTPROCESS, ROLE_SUMMARY} & set(target.roles))
    if needs_chat and target.ctype != "elevenlabs":
        try:
            from . import cloud_llm
            cloud_llm.invalidate_discover_cache()
            chat_models = list(cloud_llm.discover_chat_models(
                target.api_key, target.base_url or None) or [])
        except Exception as exc:  # noqa: BLE001
            log.warning("startup check: discover_chat_models(%s) не удался: %s", target.title, exc)
    return stt_models, chat_models


def run_check(
    target,
    *,
    attempt: int = 1,
    network_probe: Optional[Callable] = None,
    http_get: Optional[Callable] = None,
):
    """Проверка + (при успехе) получение списков моделей. Выполняется в воркере."""
    outcome = probe_target(target, attempt=attempt, network_probe=network_probe, http_get=http_get)
    if outcome.ok:
        outcome.stt_models, outcome.chat_models = discover_for_target(target)
        log.info("Стартовая проверка [попытка %d] %s: моделей STT=%d, chat=%d",
                 attempt, target.title, len(outcome.stt_models), len(outcome.chat_models))
    return outcome


def _log_attempt(attempt: int, outcome, decision: str) -> None:
    """AC 7: время (даёт формат журнала), адрес, код ответа или тип сбоя, решение."""
    log.info(
        "Стартовая проверка [попытка %d/%d] %s -> %s (%.1fс); решение: %s",
        attempt, total_attempts(), outcome.target.probe_url, outcome.event,
        outcome.elapsed, decision,
    )
