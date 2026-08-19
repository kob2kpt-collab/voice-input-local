# -*- coding: utf-8 -*-
r"""Регресс-тесты US-072: стартовая проверка не пугает ложной ошибкой ключа.

Баг: при автозапуске вместе с Windows сеть и корпоративный прокси ещё
поднимаются, а программа делала ЕДИНСТВЕННУЮ проверку соединения. Она падала,
и пользователь получал «Неверный API-ключ» на исправном ключе — плюс выбранная
облачная модель молча подменялась запасной.

Что закреплено тестами:

* первая неудача молчит — ни трея, ни строки состояния (AC 2);
* повторы идут по лестнице 5 / 10 / 20 секунд (AC 1);
* успех на любом круге тихо обновляет реестр моделей (AC 4);
* сообщение появляется один раз, когда повторы исчерпаны, и текст
  соответствует настоящей причине (AC 2, AC 3);
* «Неверный API-ключ» пишется ТОЛЬКО при ответе сервиса 401/403 (AC 3);
* cfg.selected_model не меняется ни в одном сценарии (AC 5);
* проверяются подключения из реестра cloud_connections (AC 6);
* по каждой попытке в журнал уходит адрес, код ответа или тип сбоя и
  принятое решение (AC 7);
* таймер повторов — атрибут окна, а воркер попадает в _shutdown_workers,
  иначе повторится баг US-049 (висящий таймер/поток при выходе).

Тест headless: сеть не дёргается (проверка соединения подменена), Qt-виджеты
не создаются. Запуск (в venv приложения):
    .venv\Scripts\python.exe tests\test_us072_startup_check.py
"""
from __future__ import annotations

import ast
import os
import sys
import tempfile
import types
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["LOCALAPPDATA"] = tempfile.gettempdir()

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from voice_input_app import cloud_startup  # noqa: E402
from voice_input_app.config import (  # noqa: E402
    CONNECTION_TYPE_ELEVENLABS,
    CONNECTION_TYPE_OPENAI,
    AppConfig,
    CloudConnection,
)

UI_PATH = REPO_ROOT / "voice_input_app" / "ui.py"
CLOUDRU_URL = "https://foundation-models.api.cloud.ru/v1"
GROQ_URL = "https://api.groq.com/openai/v1"

STARTUP_METHODS = (
    "start_initial_cloud_discover",
    "_run_startup_cloud_check",
    "_spawn_startup_check_worker",
    "_on_startup_cloud_check_done",
    "_apply_startup_success",
    "_clear_startup_check_error",
    "_notify_startup_check_failed",
)


# ── вспомогательное ──────────────────────────────────────────────────────


class FakeTimer:
    """Таймер повторов: запоминает паузы, ничего не планирует."""

    def __init__(self) -> None:
        self.started: list = []
        self.stops = 0

    def stop(self) -> None:
        self.stops += 1

    def start(self, ms: int) -> None:
        self.started.append(int(ms))


def _cfg_with_two_connections() -> AppConfig:
    cfg = AppConfig()
    cfg.cloud_connections = [
        CloudConnection(type=CONNECTION_TYPE_OPENAI, base_url=CLOUDRU_URL, api_key="KEY_A", name="Cloud.ru"),
        CloudConnection(type=CONNECTION_TYPE_ELEVENLABS, base_url="", api_key="KEY_B", name="ElevenLabs"),
    ]
    cfg.selected_model = f"cloud:{cfg.cloud_connections[0].id}:whisper-large-v3"
    cfg.cloud_fallback_model_key = "whisper:small"
    return cfg


def _make_window(cfg=None):
    """Mock-окно с настоящими методами MainWindow, которые трогает US-072."""
    from voice_input_app.ui import MainWindow

    events: list = []
    registered: list = []
    window = types.SimpleNamespace(
        events=events,
        registered=registered,
        spawned=[],
        cfg=cfg if cfg is not None else _cfg_with_two_connections(),
        _startup_check_timer=FakeTimer(),
        status_label=types.SimpleNamespace(setText=lambda t: events.append(("status", t))),
        tray=types.SimpleNamespace(
            showMessage=lambda *a, **kw: events.append(("tray", a[1] if len(a) > 1 else "")),
        ),
        models=types.SimpleNamespace(
            set_cloud_models=lambda *a: registered.append(tuple(a)),
        ),
        refresh_available_models_combo=lambda *a, **kw: events.append(("refresh_models", "")),
        _apply_cloud_models_to_settings_combo=lambda p, m: events.append(("legacy_combo", p)),
        _on_initial_postprocess_check_done=lambda ok, msg, models: events.append(("postprocess", ok)),
        _on_initial_summary_check_done=lambda ok, msg, models: events.append(("summary", ok)),
    )
    for name in STARTUP_METHODS:
        setattr(window, name, types.MethodType(getattr(MainWindow, name), window))
    # Воркер не запускаем: сеть в тестах не дёргаем, попытку эмулируем вручную.
    window._spawn_startup_check_worker = types.MethodType(
        lambda self, targets, attempt: self.spawned.append((list(targets), attempt)), window
    )
    return window


def _outcome(target, *, ok=False, kind="", status=None, stt=(), chat=()):
    out = cloud_startup.CheckOutcome(target=target)
    out.ok = ok
    out.kind = kind
    out.status_code = status
    out.stt_models = list(stt)
    out.chat_models = list(chat)
    return out


def _messages(window) -> list:
    return [kind for kind, _ in window.events if kind in ("status", "tray")]


def _method_source(name: str) -> str:
    src = UI_PATH.read_text(encoding="utf-8")
    start = src.index(f"    def {name}(self")
    end = src.find("\n    def ", start + 10)
    return src[start: end if end != -1 else len(src)]


# ── AC 6: цели берутся из реестра подключений ────────────────────────────


def test_targets_cover_connection_registry() -> None:
    cfg = _cfg_with_two_connections()
    # Устаревшие поля указывают на тот же эндпоинт, что и подключение A.
    cfg.openai_stt_api_key = "KEY_A"
    cfg.openai_stt_base_url = CLOUDRU_URL

    targets = cloud_startup.build_targets(cfg)

    assert len(targets) == 2, f"проверены не все подключения реестра: {targets}"
    types_ = {t.ctype for t in targets}
    assert types_ == {"openai", "elevenlabs"}, types_
    ids = {cid for t in targets for cid in t.connection_ids}
    assert ids == {c.id for c in cfg.cloud_connections}, "цель не привязана к подключению"


def test_same_endpoint_is_checked_once() -> None:
    """Двойной проверки одного эндпоинта быть не должно (общий ключ у функций)."""
    cfg = AppConfig()
    shared = dict(type=CONNECTION_TYPE_OPENAI, base_url=GROQ_URL, api_key="KEY")
    cfg.cloud_connections = [
        CloudConnection(name="STT", **shared),
        CloudConnection(name="Постобработка", **shared),
    ]
    cfg.postprocess_enabled = True
    cfg.postprocess_connection_id = cfg.cloud_connections[1].id

    targets = cloud_startup.build_targets(cfg)

    assert len(targets) == 1, f"один эндпоинт проверяется дважды: {targets}"
    assert len(targets[0].connection_ids) == 2, targets[0].connection_ids
    assert cloud_startup.ROLE_POSTPROCESS in targets[0].roles, targets[0].roles


def test_connection_without_key_is_skipped() -> None:
    cfg = AppConfig()
    cfg.cloud_connections = [CloudConnection(type=CONNECTION_TYPE_OPENAI, base_url=GROQ_URL, api_key="", name="Пустое")]
    assert cloud_startup.build_targets(cfg) == []


def test_legacy_fields_used_only_when_registry_empty() -> None:
    """Реестр пуст (config правился руками) — запасной источник ещё работает."""
    cfg = AppConfig()
    cfg.cloud_connections = []
    cfg.openai_stt_api_key = "KEY_A"
    cfg.openai_stt_base_url = GROQ_URL

    targets = cloud_startup.build_targets(cfg)

    assert len(targets) == 1 and targets[0].api_key == "KEY_A", targets
    assert targets[0].connection_ids == (), "у запасной цели не должно быть id подключения"


# ── AC 1: сеть и лестница повторов ───────────────────────────────────────


def test_request_waits_for_network() -> None:
    """Пока сеть не готова, HTTP-запрос не отправляется вообще."""
    target = cloud_startup.CheckTarget(ctype="openai", base_url=GROQ_URL, api_key="K", name="Groq")
    calls = []

    outcome = cloud_startup.probe_target(
        target,
        network_probe=lambda host: False,
        http_get=lambda url, headers: calls.append(url),
    )

    assert calls == [], "запрос ушёл до готовности сети"
    assert outcome.ok is False and outcome.kind == cloud_startup.FAIL_NO_NETWORK, outcome
    assert outcome.status_code is None, "кода ответа быть не может — запроса не было"


def test_retry_ladder_is_5_10_20() -> None:
    assert cloud_startup.RETRY_DELAYS_MS == (5000, 10000, 20000)
    assert cloud_startup.next_retry_delay_ms(1) == 5000
    assert cloud_startup.next_retry_delay_ms(2) == 10000
    assert cloud_startup.next_retry_delay_ms(3) == 20000
    assert cloud_startup.next_retry_delay_ms(4) is None, "повторы обязаны заканчиваться"
    assert cloud_startup.total_attempts() == 4


# ── AC 2: молчание до исчерпания повторов ────────────────────────────────


def test_first_failure_is_silent() -> None:
    window = _make_window()
    window.start_initial_cloud_discover()
    targets, attempt = window.spawned[0]
    assert attempt == 1 and len(targets) == 2

    window._on_startup_cloud_check_done([
        _outcome(t, kind=cloud_startup.FAIL_NO_NETWORK) for t in targets
    ])

    assert _messages(window) == [], f"первая же неудача что-то показала: {window.events}"
    assert window._startup_check_timer.started == [5000], window._startup_check_timer.started


def test_message_only_after_ladder_is_exhausted() -> None:
    window = _make_window()
    window.start_initial_cloud_discover()
    targets = window.spawned[0][0]

    for attempt in range(1, cloud_startup.total_attempts() + 1):
        window._startup_check_attempt = attempt
        window._on_startup_cloud_check_done([
            _outcome(t, kind=cloud_startup.FAIL_NO_NETWORK) for t in targets
        ])
        if attempt < cloud_startup.total_attempts():
            assert _messages(window) == [], f"сообщение на попытке {attempt}: {window.events}"

    kinds = _messages(window)
    assert kinds.count("tray") == 1, f"уведомлений в трее не ровно одно: {window.events}"
    assert kinds.count("status") == 1, f"строк состояния не ровно одна: {window.events}"
    assert window._startup_check_timer.started == [5000, 10000, 20000], window._startup_check_timer.started
    text = [t for k, t in window.events if k == "tray"][0]
    assert "сеть недоступна" in text, text
    assert "Неверный API-ключ" not in text, f"ложная ошибка ключа при отсутствии сети: {text}"


# ── AC 3: текст соответствует причине ────────────────────────────────────


def test_invalid_key_text_only_for_401_403() -> None:
    target = cloud_startup.CheckTarget(ctype="openai", base_url=GROQ_URL, api_key="K", name="Groq")
    for status in (401, 403):
        outcome = _outcome(target, kind=cloud_startup.classify_status(status), status=status)
        assert outcome.kind == cloud_startup.FAIL_AUTH, status
        assert "Неверный API-ключ" in cloud_startup.failure_text(outcome), status

    not_auth = {
        cloud_startup.FAIL_NO_NETWORK: "сеть недоступна",
        cloud_startup.FAIL_TIMEOUT: "не пришёл вовремя",
        cloud_startup.FAIL_PROXY: "прокси",
        cloud_startup.FAIL_TLS: "защищённое соединение",
    }
    for kind, fragment in not_auth.items():
        text = cloud_startup.failure_text(_outcome(target, kind=kind))
        assert fragment in text, (kind, text)
        assert "Неверный API-ключ" not in text, (kind, text)

    http_500 = _outcome(target, kind=cloud_startup.classify_status(500), status=500)
    assert http_500.kind == cloud_startup.FAIL_HTTP
    text = cloud_startup.failure_text(http_500)
    assert "HTTP 500" in text and "Неверный API-ключ" not in text, text


def test_exception_classification() -> None:
    """Причина берётся из типа исключения, а не из русского текста verify_*."""

    class ConnErr(Exception):
        pass
    ConnErr.__name__ = "ConnectionError"

    class TimeoutErr(ConnErr):
        pass
    TimeoutErr.__name__ = "Timeout"

    class ProxyErr(ConnErr):
        pass
    ProxyErr.__name__ = "ProxyError"

    class SslErr(ConnErr):
        pass
    SslErr.__name__ = "SSLError"

    assert cloud_startup.classify_exception(ConnErr()) == cloud_startup.FAIL_NO_NETWORK
    assert cloud_startup.classify_exception(TimeoutErr()) == cloud_startup.FAIL_TIMEOUT
    assert cloud_startup.classify_exception(ProxyErr()) == cloud_startup.FAIL_PROXY
    assert cloud_startup.classify_exception(SslErr()) == cloud_startup.FAIL_TLS
    assert cloud_startup.classify_exception(ValueError("?")) == cloud_startup.FAIL_UNKNOWN


def test_real_requests_exceptions_are_classified() -> None:
    """Настоящие исключения requests должны попадать в те же корзины."""
    try:
        import requests
    except ImportError:
        return
    assert cloud_startup.classify_exception(requests.exceptions.ProxyError()) == cloud_startup.FAIL_PROXY
    assert cloud_startup.classify_exception(requests.exceptions.SSLError()) == cloud_startup.FAIL_TLS
    assert cloud_startup.classify_exception(requests.exceptions.ConnectTimeout()) == cloud_startup.FAIL_TIMEOUT
    assert cloud_startup.classify_exception(requests.exceptions.ReadTimeout()) == cloud_startup.FAIL_TIMEOUT
    assert cloud_startup.classify_exception(requests.exceptions.ConnectionError()) == cloud_startup.FAIL_NO_NETWORK


def test_http_401_from_response_is_auth() -> None:
    target = cloud_startup.CheckTarget(ctype="openai", base_url=GROQ_URL, api_key="K", name="Groq")
    response = types.SimpleNamespace(status_code=401, text="{'error': 'invalid key'}")

    outcome = cloud_startup.probe_target(
        target, network_probe=lambda host: True, http_get=lambda url, headers: response
    )

    assert outcome.kind == cloud_startup.FAIL_AUTH and outcome.status_code == 401
    assert "Неверный API-ключ" in cloud_startup.failure_text(outcome)


# ── AC 4: успех тихо обновляет реестр и снимает сообщение ────────────────


def test_success_on_second_attempt_is_quiet() -> None:
    window = _make_window()
    window.start_initial_cloud_discover()
    targets = window.spawned[0][0]

    window._on_startup_cloud_check_done([_outcome(t, kind=cloud_startup.FAIL_TIMEOUT) for t in targets])
    assert _messages(window) == [], window.events

    # Повторная попытка — успех.
    window._run_startup_cloud_check()
    retry_targets, attempt = window.spawned[1]
    assert attempt == 2 and len(retry_targets) == 2
    window._on_startup_cloud_check_done([
        _outcome(t, ok=True, stt=["whisper-large-v3"]) for t in retry_targets
    ])

    assert _messages(window) == [], f"успешный повтор что-то показал: {window.events}"
    assert len(window.registered) == 2, f"реестр моделей не обновлён: {window.registered}"
    assert any(k == "refresh_models" for k, _ in window.events), window.events
    assert window._startup_check_targets == [], "успешные цели остались в очереди повторов"


def test_success_clears_previous_error_message() -> None:
    window = _make_window()
    window.start_initial_cloud_discover()
    target = window.spawned[0][0][0]
    window._startup_check_attempt = cloud_startup.total_attempts()
    window._on_startup_cloud_check_done([_outcome(target, kind=cloud_startup.FAIL_NO_NETWORK)])
    assert window._startup_check_error_shown is True
    window.events.clear()

    window._apply_startup_success(_outcome(target, ok=True, stt=["whisper-1"]))

    assert window._startup_check_error_shown is False, "сообщение об ошибке не снято"
    assert ("tray" not in _messages(window)), window.events


def test_empty_discover_does_not_wipe_known_models() -> None:
    """Разовый пустой ответ discover не должен очищать список моделей диктовки."""
    window = _make_window()
    window.start_initial_cloud_discover()
    target = window.spawned[0][0][0]

    window._apply_startup_success(_outcome(target, ok=True, stt=[]))

    assert window.registered == [], "пустой ответ затёр реестр моделей"


def test_partial_success_retries_only_failed() -> None:
    window = _make_window()
    window.start_initial_cloud_discover()
    ok_target, bad_target = window.spawned[0][0]

    window._on_startup_cloud_check_done([
        _outcome(ok_target, ok=True, stt=["whisper-1"]),
        _outcome(bad_target, kind=cloud_startup.FAIL_TIMEOUT),
    ])

    assert _messages(window) == [], window.events
    assert window._startup_check_targets == [bad_target], window._startup_check_targets
    window._run_startup_cloud_check()
    assert window.spawned[1][0] == [bad_target], "повтор ушёл по уже прошедшей цели"


def test_chat_models_reach_postprocess_and_summary() -> None:
    cfg = AppConfig()
    conn = CloudConnection(type=CONNECTION_TYPE_OPENAI, base_url=GROQ_URL, api_key="KEY", name="Groq")
    cfg.cloud_connections = [conn]
    cfg.postprocess_enabled = True
    cfg.postprocess_connection_id = conn.id
    cfg.summary_mode = "cloud"
    cfg.summary_connection_id = conn.id
    window = _make_window(cfg)

    window.start_initial_cloud_discover()
    target = window.spawned[0][0][0]
    window._on_startup_cloud_check_done([
        _outcome(target, ok=True, stt=["whisper-large-v3"], chat=["llama-3.3-70b"]),
    ])

    kinds = [k for k, _ in window.events]
    assert "postprocess" in kinds and "summary" in kinds, window.events
    assert _messages(window) == [], window.events


# ── AC 5: выбранная модель не подменяется ────────────────────────────────


def test_selected_model_never_changes() -> None:
    for failures in (1, 2, 3, cloud_startup.total_attempts()):
        window = _make_window()
        before = window.cfg.selected_model
        window.start_initial_cloud_discover()
        targets = window.spawned[0][0]
        for attempt in range(1, failures + 1):
            window._startup_check_attempt = attempt
            window._on_startup_cloud_check_done([
                _outcome(t, kind=cloud_startup.FAIL_NO_NETWORK) for t in targets
            ])
        assert window.cfg.selected_model == before, f"модель подменена после {failures} неудач"


def test_startup_code_does_not_touch_selected_model() -> None:
    """Статический guard: подмена модели убрана из стартовой проверки насовсем."""
    src = UI_PATH.read_text(encoding="utf-8")
    assert "def _on_initial_cloud_check_done" not in src, (
        "старый обработчик с автопереключением на запасную модель всё ещё в ui.py"
    )
    tree = ast.parse(src)
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name not in STARTUP_METHODS:
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Assign):
                for tgt in inner.targets:
                    if isinstance(tgt, ast.Attribute) and tgt.attr == "selected_model":
                        bad.append((node.name, getattr(inner, "lineno", "?")))
            if isinstance(inner, ast.Attribute) and inner.attr == "cloud_fallback_model_key":
                bad.append((node.name, getattr(inner, "lineno", "?")))
    assert not bad, f"стартовая проверка снова трогает выбранную модель: {bad}"


# ── AC 7: журнал попыток ─────────────────────────────────────────────────


def test_attempt_is_logged_with_address_event_and_decision() -> None:
    import logging

    records: list = []

    class Catcher(logging.Handler):
        def emit(self, record):  # noqa: D102
            records.append(record.getMessage())

    handler = Catcher()
    cloud_startup.log.addHandler(handler)
    try:
        target = cloud_startup.CheckTarget(ctype="openai", base_url=GROQ_URL, api_key="K", name="Groq")
        cloud_startup.probe_target(
            target,
            attempt=2,
            network_probe=lambda host: True,
            http_get=lambda url, headers: types.SimpleNamespace(status_code=500, text="boom"),
        )
    finally:
        cloud_startup.log.removeHandler(handler)

    assert records, "попытка не попала в журнал"
    line = records[-1]
    assert target.probe_url in line, line          # адрес
    assert "HTTP 500" in line, line                # код ответа
    assert "решение" in line, line                 # принятое решение
    assert "попытка 2/4" in line, line             # номер попытки


# ── US-049: таймер и воркер не мешают выходу ─────────────────────────────


def test_timer_is_window_attribute_and_worker_is_awaited() -> None:
    src = UI_PATH.read_text(encoding="utf-8")
    body = _method_source("_on_startup_cloud_check_done")
    assert "self._startup_check_timer = timer" in body, (
        "таймер повторов не хранится атрибутом окна — really_quit его не погасит (US-049)"
    )
    assert '"_startup_check_worker"' in src, (
        "воркер стартовой проверки не попал в _shutdown_workers (US-049)"
    )
    spawn = _method_source("_spawn_startup_check_worker")
    assert "self._startup_check_worker = worker" in spawn, "нет ссылки на воркер (anti-GC)"


def test_timer_is_stopped_by_quit_sweep() -> None:
    """Обход self.__dict__ в really_quit обязан гасить наш таймер."""
    from PySide6.QtCore import QCoreApplication, QTimer

    _app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    window = _make_window()
    window._startup_check_timer = QTimer()
    window._startup_check_timer.setSingleShot(True)
    window._startup_check_timer.start(20000)
    assert window._startup_check_timer.isActive()

    for value in list(window.__dict__.values()):   # ровно то, что делает really_quit
        if isinstance(value, QTimer):
            value.stop()

    assert not window._startup_check_timer.isActive(), "таймер повторов пережил выход"


def _run() -> None:
    tests = [
        test_targets_cover_connection_registry,
        test_same_endpoint_is_checked_once,
        test_connection_without_key_is_skipped,
        test_legacy_fields_used_only_when_registry_empty,
        test_request_waits_for_network,
        test_retry_ladder_is_5_10_20,
        test_first_failure_is_silent,
        test_message_only_after_ladder_is_exhausted,
        test_invalid_key_text_only_for_401_403,
        test_exception_classification,
        test_real_requests_exceptions_are_classified,
        test_http_401_from_response_is_auth,
        test_success_on_second_attempt_is_quiet,
        test_success_clears_previous_error_message,
        test_empty_discover_does_not_wipe_known_models,
        test_partial_success_retries_only_failed,
        test_chat_models_reach_postprocess_and_summary,
        test_selected_model_never_changes,
        test_startup_code_does_not_touch_selected_model,
        test_attempt_is_logged_with_address_event_and_decision,
        test_timer_is_window_attribute_and_worker_is_awaited,
        test_timer_is_stopped_by_quit_sweep,
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print("US-072 regression: ALL PASS")


if __name__ == "__main__":
    _run()
