# -*- coding: utf-8 -*-
r"""Регресс-тесты US-073: внешние модели Cloud.ru не попадают в списки выбора.

Заказчик (ИТ-директор) требует, чтобы сотрудник не мог выбрать модель, при
работе с которой данные уходят за пределы инфраструктуры Cloud.ru (152-ФЗ).
Признак размещения даёт сам сервис: `metadata.provider` в ответе /v1/models
(`cloud.ru` — внутренняя, `external` — внешняя).

Что закреплено тестами:

* AC 1/AC 3 — признак берётся из ответа сервиса, а НЕ из имени модели
  (`openai/gpt-oss-120b` внутренняя, `openai/gpt-oss-20b` внешняя,
  `openai/whisper-large-v3` внутренняя);
* AC 2 — при включённом флажке внешних моделей нет ни в одном списке;
* AC 4 — флажок задаётся отдельно для подключения, по умолчанию включён,
  включается и для уже настроенных подключений при обновлении (миграция);
* AC 5 — подключение без признака размещения работает как раньше, флажок
  недоступен;
* AC 6 — выбранная скрытая модель помечена недоступной и блокирует запуск;
* AC 7 — если сервис перестал отдавать признак, список НЕ расширяется;
* AC 8 — «безопасный эндпоинт» US-018 признаётся только при включённом флажке;
* AC 9 — число скрытых моделей видно в журнале;
* TASK-365 — тип модели берётся из `metadata.type` с откатом на разбор имени;
* контракт US-073 — фильтр стоит чокпоинтом в реестре моделей, зарегистрировать
  модель в обход него нельзя.

Тест headless: сеть не вызывается (ответ /v1/models подменён фикстурой).
Запуск (в venv приложения):
    .venv\Scripts\python.exe tests\test_us073_cloudru_filter.py
"""
from __future__ import annotations

import ast
import json
import logging
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

from voice_input_app import cloud_llm, cloud_placement, cloud_stt, models as models_module  # noqa: E402
from voice_input_app.config import (  # noqa: E402
    CONNECTION_TYPE_ELEVENLABS,
    CONNECTION_TYPE_OPENAI,
    AppConfig,
    CloudConnection,
)
from voice_input_app.models import ModelManager  # noqa: E402

UI_PATH = REPO_ROOT / "voice_input_app" / "ui.py"
MODELS_PATH = REPO_ROOT / "voice_input_app" / "models.py"

CLOUDRU_URL = "https://foundation-models.api.cloud.ru/v1"
OTHER_URL = "https://api.groq.com/openai/v1"

# Фикстура ответа сервиса. Форма и значения — по итогам исследования боевого
# ключа (TASK-363): у всех моделей ОДИН хост, различает их только
# metadata.provider; имена внутренней и внешней моделей похожи специально.
CLOUDRU_PAYLOAD = {
    "object": "list",
    "data": [
        {"id": "openai/whisper-large-v3", "object": "model",
         "metadata": {"provider": "cloud.ru", "type": "audio-to-text"}},
        {"id": "openai/gpt-oss-120b", "object": "model",
         "metadata": {"provider": "cloud.ru", "type": "llm"}},
        {"id": "openai/gpt-oss-20b", "object": "model",
         "metadata": {"provider": "external", "type": "llm"}},
        # Ключевой случай: ВНЕШНЯЯ модель распознавания речи — по имени
        # неотличима от внутренней, а в список диктовки попасть не должна.
        {"id": "vendor/whisper-turbo-v9", "object": "model",
         "metadata": {"provider": "external", "type": "audio-to-text"}},
        {"id": "ai-sage/giga-embeddings", "object": "model",
         "metadata": {"provider": "cloud.ru", "type": "embedder"}},
    ],
}

# Ответ сервиса, который признак размещения БОЛЬШЕ не сообщает (AC 7).
PAYLOAD_WITHOUT_PLACEMENT = {
    "object": "list",
    "data": [
        {"id": "openai/whisper-large-v3", "object": "model"},
        {"id": "vendor/whisper-turbo-v9", "object": "model"},
        {"id": "brand-new/whisper-x", "object": "model"},
    ],
}

# Сторонний OpenAI-совместимый сервис: ни provider, ни type (AC 5, TASK-365).
THIRD_PARTY_PAYLOAD = {
    "object": "list",
    "data": [
        {"id": "whisper-1", "object": "model"},
        {"id": "gpt-4o-mini", "object": "model"},
    ],
}


# ---------- вспомогательное ----------


def _reset_state() -> None:
    cloud_placement.forget_endpoints()
    cloud_placement.clear_policies()
    cloud_stt.invalidate_discover_cache()
    cloud_llm.invalidate_discover_cache()
    models_module._CLOUD_MODELS_REGISTRY.clear()


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


def _with_fake_models_endpoint(payload, fn):
    """Выполнить fn(), подменив GET /v1/models фикстурой (сети нет)."""
    original = cloud_stt.requests.get
    cloud_stt.requests.get = lambda *a, **kw: _FakeResponse(payload)
    try:
        return fn()
    finally:
        cloud_stt.requests.get = original


def _cloudru_connection(**overrides) -> CloudConnection:
    conn = CloudConnection(
        name="Cloud.ru",
        type=CONNECTION_TYPE_OPENAI,
        base_url=CLOUDRU_URL,
        api_key="KEY",
    )
    infos = cloud_placement.parse_models_payload(CLOUDRU_PAYLOAD)
    snap = cloud_placement.snapshot_from_infos(infos)
    conn.discovered_models = [i.id for i in infos]
    conn.model_placement = dict(snap.placement)
    conn.model_types = dict(snap.types)
    conn.reports_model_placement = True
    for k, v in overrides.items():
        setattr(conn, k, v)
    return conn


def _cfg_with(conn) -> AppConfig:
    cfg = AppConfig()
    cfg.cloud_connections = [conn]
    return cfg


def _registry_model_ids(conn) -> list:
    prefix = f"cloud:{conn.id}:"
    return sorted(k[len(prefix):] for k in models_module._CLOUD_MODELS_REGISTRY if k.startswith(prefix))


# ---------- AC 1, AC 3: признак из ответа сервиса, а не из имени ----------


def test_placement_comes_from_metadata_not_from_name():
    _reset_state()
    infos = {i.id: i for i in cloud_placement.parse_models_payload(CLOUDRU_PAYLOAD)}

    assert infos["openai/whisper-large-v3"].placement == cloud_placement.PLACEMENT_INTERNAL
    assert infos["openai/gpt-oss-120b"].placement == cloud_placement.PLACEMENT_INTERNAL
    assert infos["openai/gpt-oss-20b"].placement == cloud_placement.PLACEMENT_EXTERNAL

    # Имя признаком не является: две модели одного семейства разведены
    # по разным сторонам периметра.
    assert infos["openai/gpt-oss-120b"].placement != infos["openai/gpt-oss-20b"].placement, (
        "фильтр разошёлся с фактом: модели одного семейства должны различаться "
        "только по metadata.provider"
    )
    # И наоборот: внешняя модель с «внутренним» на вид именем — внешняя.
    assert infos["vendor/whisper-turbo-v9"].placement == cloud_placement.PLACEMENT_EXTERNAL

    # Незнакомое значение provider не считается внутренним (иначе фильтр можно
    # было бы обойти новым значением на стороне сервиса).
    assert cloud_placement.classify_provider("some-new-vendor") == cloud_placement.PLACEMENT_UNKNOWN
    assert cloud_placement.classify_provider("") == cloud_placement.PLACEMENT_UNKNOWN


# ---------- AC 2: ни один список не содержит внешних моделей ----------


def test_registry_hides_external_models():
    _reset_state()
    conn = _cloudru_connection()
    mgr = ModelManager()
    mgr.refresh_cloud_models(_cfg_with(conn))

    ids = _registry_model_ids(conn)
    assert ids == ["openai/whisper-large-v3"], f"в реестре не только внутренняя STT-модель: {ids}"
    assert "vendor/whisper-turbo-v9" not in ids, "внешняя STT-модель попала в список диктовки"

    # available_model_keys / cloud_model_keys — источники всех списков UI
    # (диктовка, файлы, быстрый выбор в плашке).
    assert all("vendor/whisper-turbo-v9" not in k for k in mgr.cloud_model_keys())
    assert all("gpt-oss-20b" not in k for k in mgr.available_model_keys())

    # Скрытая модель недоступна для выбора и для запуска.
    hidden_key = f"cloud:{conn.id}:vendor/whisper-turbo-v9"
    assert mgr.is_available(hidden_key) is False


def test_llm_lists_hide_external_models():
    """AC 3: улучшение расшифровки и суммаризация — тоже списки подключения."""
    _reset_state()
    conn = _cloudru_connection()
    allowed, hidden = cloud_placement.filter_connection_models(conn, conn.discovered_models)
    assert "openai/gpt-oss-120b" in allowed, "внутренняя LLM пропала из списка"
    assert "openai/gpt-oss-20b" not in allowed, "внешняя LLM осталась в списке"
    assert hidden["external"] == 2, hidden


def test_discover_applies_policy_offline():
    """Фильтр действует и на промежуточный список discover (не только в реестре)."""
    _reset_state()
    conn = _cloudru_connection()
    ModelManager().refresh_cloud_models(_cfg_with(conn))  # публикует политику

    got = _with_fake_models_endpoint(
        CLOUDRU_PAYLOAD,
        lambda: cloud_stt.discover_models("openai", "KEY", CLOUDRU_URL, use_cache=False),
    )
    assert got == ["openai/whisper-large-v3"], f"discover вернул внешние модели: {got}"


# ---------- флажок выключен ----------


def test_filter_off_returns_all_models():
    _reset_state()
    conn = _cloudru_connection(only_internal_models=False)
    mgr = ModelManager()
    mgr.refresh_cloud_models(_cfg_with(conn))

    ids = _registry_model_ids(conn)
    assert "vendor/whisper-turbo-v9" in ids, "при выключенном флажке внешняя STT-модель должна быть доступна"
    assert "openai/whisper-large-v3" in ids
    # LLM-списки тоже без ограничений.
    allowed, hidden = cloud_placement.filter_connection_models(conn, conn.discovered_models)
    assert hidden == {"external": 0, "unknown": 0} and len(allowed) == len(conn.discovered_models)


# ---------- AC 5: подключение без признака размещения ----------


def test_connection_without_placement_signal_behaves_as_before():
    _reset_state()
    conn = CloudConnection(name="Сторонний", type=CONNECTION_TYPE_OPENAI, base_url=OTHER_URL, api_key="KEY")
    conn.discovered_models = ["whisper-1", "gpt-4o-mini"]
    # Флажок включён по умолчанию, но сервис размещение не сообщает.
    assert conn.only_internal_models is True
    assert conn.reports_model_placement is False

    mgr = ModelManager()
    mgr.refresh_cloud_models(_cfg_with(conn))
    ids = _registry_model_ids(conn)
    assert ids == ["whisper-1"], f"поведение сторонних сервисов изменилось: {ids}"
    assert cloud_placement.connection_reports_placement(conn) is False
    assert cloud_placement.connection_hidden_reason(conn, "whisper-1") == ""

    # ElevenLabs размещение не сообщает вовсе — фильтр не должен его трогать.
    el = CloudConnection(name="ElevenLabs", type=CONNECTION_TYPE_ELEVENLABS, base_url="", api_key="KEY")
    el.discovered_models = ["scribe_v1"]
    _reset_state()
    ModelManager().refresh_cloud_models(_cfg_with(el))
    assert _registry_model_ids(el) == ["scribe_v1"]


def test_checkbox_disabled_without_placement_signal():
    """Статический guard AC 5: флажок недоступен, если признака нет, и рядом
    выводится пояснение."""
    src = _method_source(UI_PATH, "_update_safe_enabled")
    assert "self.only_internal_check.setEnabled(is_openai and reports)" in src, (
        "флажок больше не блокируется для подключений без признака размещения"
    )
    assert "placement_hint" in src, "пропало пояснение рядом с недоступным флажком"
    # AC 8: пометка безопасного эндпоинта требует включённого фильтра.
    assert "only_internal_on" in src, "пометка безопасного эндпоинта снова не зависит от фильтра"


# ---------- AC 7: признак пропал — список не расширяется ----------


def test_lost_placement_signal_does_not_expand_list():
    _reset_state()
    conn = _cloudru_connection()
    # Сервис перестал отдавать metadata.provider (и вернул новую модель).
    infos = cloud_placement.parse_models_payload(PAYLOAD_WITHOUT_PLACEMENT)
    snap = cloud_placement.remember_endpoint_models(CLOUDRU_URL, infos)
    assert snap.reports_placement is False
    conn.discovered_models = [i.id for i in infos]  # как после нового discover

    mgr = ModelManager()
    mgr.refresh_cloud_models(_cfg_with(conn))
    ids = _registry_model_ids(conn)
    assert ids == ["openai/whisper-large-v3"], (
        f"после пропажи признака список расширился внешними/неизвестными моделями: {ids}"
    )
    assert cloud_placement.connection_hidden_reason(conn, "brand-new/whisper-x") == "unknown"
    assert cloud_placement.connection_hidden_reason(conn, "vendor/whisper-turbo-v9") == "external"

    # И предупреждение об этом пользователю есть (AC 7).
    src = _method_source(UI_PATH, "_absorb_placement_snapshot")
    assert "больше не сообщает размещение" in src, "пропало предупреждение о пропаже признака"


# ---------- AC 4: миграция уже настроенных подключений ----------


def test_migration_enables_filter_for_existing_connections():
    old_config = {
        "selected_model": "whisper:small",
        "cloud_connections": [
            {
                "id": "conn-old",
                "name": "Cloud.ru",
                "type": "openai",
                "base_url": CLOUDRU_URL,
                "api_key": "KEY",
                "discovered_models": ["openai/whisper-large-v3", "openai/gpt-oss-20b"],
            }
        ],
    }
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps(old_config, ensure_ascii=False), encoding="utf-8")
        cfg = AppConfig.load(path)

    assert len(cfg.cloud_connections) == 1, "подключение потерялось при миграции"
    conn = cfg.cloud_connections[0]
    assert conn.only_internal_models is True, "флажок не включился для уже настроенного подключения"
    assert conn.reports_model_placement is False, "признак размещения не выдуман из воздуха"
    assert conn.model_placement == {} and conn.model_types == {}
    assert conn.discovered_models == ["openai/whisper-large-v3", "openai/gpt-oss-20b"]

    # До первой проверки соединения признаков нет — фильтр бездействует
    # (список не пустеет), но включённый флажок уже сохранён.
    _reset_state()
    ModelManager().refresh_cloud_models(cfg)
    assert _registry_model_ids(conn) == ["openai/whisper-large-v3"]


# ---------- AC 6: скрытая выбранная модель ----------


def test_hidden_selected_model_is_blocked():
    from voice_input_app.ui import MainWindow

    _reset_state()
    conn = _cloudru_connection()
    cfg = _cfg_with(conn)
    hidden_key = f"cloud:{conn.id}:vendor/whisper-turbo-v9"
    cfg.selected_model = hidden_key
    fake = types.SimpleNamespace(cfg=cfg)
    # _placement_hidden_message опирается на _placement_hidden_reason — привязываем
    # настоящие методы к подставному окну (Qt-окно для проверки не нужно).
    fake._placement_hidden_reason = types.MethodType(MainWindow._placement_hidden_reason, fake)
    fake._cloud_model_label = types.MethodType(MainWindow._cloud_model_label, fake)

    reason = MainWindow._placement_hidden_reason(fake, hidden_key)
    assert reason == "external", f"скрытая модель не распознана как скрытая: {reason!r}"
    message = MainWindow._placement_hidden_message(fake, hidden_key)
    assert "скрыта фильтром подключения" in message
    assert "Cloud.ru" in message

    # Локальная и внутренняя модели не блокируются.
    assert MainWindow._placement_hidden_reason(fake, "whisper:small") == ""
    assert MainWindow._placement_hidden_reason(fake, f"cloud:{conn.id}:openai/whisper-large-v3") == ""

    # Гейты запуска действительно вызывают блокировку.
    for method in ("start_recording", "start_file_transcription"):
        src = _method_source(UI_PATH, method)
        assert "_block_if_model_hidden_by_placement" in src, (
            f"{method} больше не проверяет фильтр размещения — регресс AC 6"
        )
    # Обхода «продолжить всё равно» в окне блокировки нет (решение владельца).
    dlg = _method_source(UI_PATH, "_show_placement_block_dialog")
    assert "Открыть настройки" in dlg
    for banned in ("Продолжить", "Всё равно", "Отключить фильтр"):
        assert banned not in dlg, f"в окно блокировки вернулся обход: {banned}"

    # Выбор НЕ подменяется молча на локальную модель.
    ensure_src = _method_source(UI_PATH, "ensure_selected_model_available")
    assert "_placement_hidden_reason" in ensure_src, (
        "скрытая модель снова молча заменяется моделью по умолчанию"
    )


# ---------- AC 8: «безопасно» только при включённом фильтре ----------


def test_safe_endpoint_requires_filter():
    from voice_input_app.ui import MainWindow

    _reset_state()
    conn = _cloudru_connection()
    cfg = _cfg_with(conn)
    cfg.cloud_internal_safe_endpoints = [CLOUDRU_URL]
    fake = types.SimpleNamespace(cfg=cfg)

    assert MainWindow._endpoint_marked_safe(fake, CLOUDRU_URL) is True

    conn.only_internal_models = False
    assert MainWindow._endpoint_marked_safe(fake, CLOUDRU_URL) is False, (
        "пометка «безопасно» показывается при выключенном фильтре — регресс AC 8"
    )


# ---------- AC 9: число скрытых моделей в журнале ----------


def test_hidden_count_is_logged_per_connection():
    _reset_state()
    conn = _cloudru_connection()
    records: list = []

    class _Catcher(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = _Catcher()
    models_module.log.addHandler(handler)
    try:
        ModelManager().refresh_cloud_models(_cfg_with(conn))
    finally:
        models_module.log.removeHandler(handler)

    line = [m for m in records if "US-073 registry" in m]
    assert line, f"в журнале нет строки о скрытых моделях: {records}"
    assert conn.id in line[0], "в журнале не видно, о каком подключении речь"
    assert "hidden by Cloud.ru filter 2" in line[0], f"неверное число скрытых моделей: {line[0]}"


# ---------- TASK-365: тип модели вместо угадывания по имени ----------


def test_model_type_replaces_name_guess():
    _reset_state()
    # Явный тип от сервиса главнее имени в обе стороны.
    assert cloud_stt.is_stt_model("openai/whisper-large-v3", "audio-to-text") is True
    assert cloud_stt.is_stt_model("openai/whisper-large-v3", "llm") is False, (
        "тип от сервиса проигрывает имени модели"
    )
    assert cloud_stt.is_stt_model("some/unnamed-model", "audio-to-text") is True
    # Нет типа — прежний разбор имени (иначе список сторонних сервисов опустеет).
    assert cloud_stt.is_stt_model("whisper-1", "") is True
    assert cloud_stt.is_stt_model("gpt-4o-mini", "") is False


def test_type_fallback_keeps_third_party_lists():
    _reset_state()
    got = _with_fake_models_endpoint(
        THIRD_PARTY_PAYLOAD,
        lambda: cloud_stt.discover_models("openai", "KEY", OTHER_URL, use_cache=False),
    )
    assert got == ["whisper-1"], f"без metadata.type список моделей диктовки сломался: {got}"

    # И в реестре тоже — через откат на имя.
    conn = CloudConnection(name="Сторонний", type=CONNECTION_TYPE_OPENAI, base_url=OTHER_URL, api_key="KEY")
    conn.discovered_models = ["whisper-1", "gpt-4o-mini"]
    _reset_state()
    ModelManager().refresh_cloud_models(_cfg_with(conn))
    assert _registry_model_ids(conn) == ["whisper-1"]


def test_type_filter_keeps_llm_out_of_dictation_list():
    _reset_state()
    conn = _cloudru_connection(only_internal_models=False)
    ModelManager().refresh_cloud_models(_cfg_with(conn))
    ids = _registry_model_ids(conn)
    assert "openai/gpt-oss-120b" not in ids, "LLM-модель попала в список диктовки"
    assert "ai-sage/giga-embeddings" not in ids, "модель эмбеддингов попала в список диктовки"


# ---------- карточка подключения и списки функций (Qt, offscreen) ----------


def _qt_app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_connection_dialog_gates_checkboxes():
    """AC 4/AC 5/AC 8 на живом диалоге, а не только по исходнику."""
    from voice_input_app.ui import ConnectionDialog

    _qt_app()
    _reset_state()
    conn = _cloudru_connection()
    dlg = ConnectionDialog(None, connection=conn, initial_safe=True)
    assert dlg.only_internal_check.isChecked() is True, "флажок должен быть включён"
    assert dlg.only_internal_check.isEnabled() is True, "признак есть — флажок должен быть доступен"
    assert dlg.safe_endpoint_check.isEnabled() is True

    dlg.only_internal_check.setChecked(False)  # AC 8
    assert dlg.safe_endpoint_check.isEnabled() is False, (
        "«безопасный эндпоинт» доступен при выключенном фильтре — регресс AC 8"
    )
    assert dlg.safe_endpoint_check.isChecked() is False

    dlg.only_internal_check.setChecked(True)
    out = dlg.result_connection()
    assert out.only_internal_models is True
    assert out.reports_model_placement is True
    assert out.model_placement.get("openai/gpt-oss-20b") == cloud_placement.PLACEMENT_EXTERNAL
    assert out.model_types.get("openai/whisper-large-v3") == "audio-to-text"
    dlg.deleteLater()

    # Подключение без признака размещения: флажок недоступен + пояснение (AC 5).
    plain = CloudConnection(name="Сторонний", type=CONNECTION_TYPE_OPENAI, base_url=OTHER_URL, api_key="K")
    dlg2 = ConnectionDialog(None, connection=plain)
    assert dlg2.only_internal_check.isEnabled() is False
    assert "не сообщает" in dlg2.placement_hint.text()
    assert dlg2.safe_endpoint_check.isEnabled() is False
    dlg2.deleteLater()


def test_llm_combo_hides_external_and_drops_hidden_current():
    """AC 3: список моделей улучшения расшифровки/суммаризации без внешних."""
    from PySide6.QtWidgets import QComboBox

    from voice_input_app.ui import MainWindow

    _qt_app()
    _reset_state()
    conn = _cloudru_connection()
    combo = QComboBox()
    combo.setEditable(True)
    MainWindow._fill_llm_model_combo(types.SimpleNamespace(), combo, conn, "openai/gpt-oss-20b")
    items = [combo.itemText(i) for i in range(combo.count())]
    assert "openai/gpt-oss-120b" in items, f"внутренняя LLM пропала из списка: {items}"
    assert "openai/gpt-oss-20b" not in items, f"внешняя LLM осталась в списке: {items}"
    assert combo.currentText() == "", "скрытая модель осталась выбранной в поле"

    # Флажок снят — прежнее поведение, модель возвращается.
    conn.only_internal_models = False
    MainWindow._fill_llm_model_combo(types.SimpleNamespace(), combo, conn, "openai/gpt-oss-20b")
    items = [combo.itemText(i) for i in range(combo.count())]
    assert "openai/gpt-oss-20b" in items and combo.currentText() == "openai/gpt-oss-20b"


# ---------- контракт: фильтр стоит чокпоинтом в реестре ----------


def test_registry_write_only_inside_checkpoint():
    """Статический guard: писать в _CLOUD_MODELS_REGISTRY можно только внутри
    _register_cloud_model — иначе фильтр можно обойти новым путём регистрации."""
    tree = ast.parse(MODELS_PATH.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for n in ast.walk(node):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if (isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name)
                            and t.value.id == "_CLOUD_MODELS_REGISTRY"
                            and node.name != "_register_cloud_model"):
                        offenders.append((node.name, n.lineno))
    assert not offenders, (
        f"регистрация облачных моделей в обход чокпоинта US-073: {offenders}"
    )


def test_set_cloud_models_cannot_bypass_filter():
    """Ответ проверки соединения тоже проходит через чокпоинт."""
    _reset_state()
    conn = _cloudru_connection()
    mgr = ModelManager()
    mgr.refresh_cloud_models(_cfg_with(conn))

    # Прямая регистрация «всех найденных моделей» (новый и старый стили вызова).
    mgr.set_cloud_models(conn.id, conn.type, list(conn.discovered_models))
    assert _registry_model_ids(conn) == ["openai/whisper-large-v3"], _registry_model_ids(conn)

    mgr.set_cloud_models("openai", list(conn.discovered_models))
    assert _registry_model_ids(conn) == ["openai/whisper-large-v3"], _registry_model_ids(conn)

    # И одиночная регистрация внешней модели возвращает причину отказа.
    assert mgr._register_cloud_model(conn.id, conn.type, "vendor/whisper-turbo-v9") == "external"
    assert mgr._register_cloud_model(conn.id, conn.type, "openai/whisper-large-v3") == ""


# ---------- совместимость сигнатур (контракт между агентами) ----------


def test_public_discover_signatures_unchanged():
    _reset_state()
    got_stt = _with_fake_models_endpoint(
        THIRD_PARTY_PAYLOAD,
        lambda: cloud_stt.discover_models("openai", "KEY", OTHER_URL, use_cache=False),
    )
    assert isinstance(got_stt, list) and all(isinstance(x, str) for x in got_stt)

    original = cloud_llm.requests.get
    cloud_llm.requests.get = lambda *a, **kw: _FakeResponse(CLOUDRU_PAYLOAD)
    try:
        all_ids = cloud_llm.discover_all_models("KEY", CLOUDRU_URL, use_cache=False)
        chat_ids = cloud_llm.discover_chat_models("KEY", CLOUDRU_URL, use_cache=False)
        detailed = cloud_llm.discover_all_models_detailed("KEY", CLOUDRU_URL, use_cache=False)
    finally:
        cloud_llm.requests.get = original

    assert all(isinstance(x, str) for x in all_ids) and "openai/gpt-oss-20b" in all_ids, (
        "полный список подключения должен оставаться полным (иначе снятие флажка "
        "не вернёт модели без повторной проверки соединения)"
    )
    assert all(isinstance(x, str) for x in chat_ids)
    assert detailed and hasattr(detailed[0], "placement"), "новая функция должна отдавать признаки"


def _method_source(path: Path, name: str) -> str:
    src = path.read_text(encoding="utf-8")
    start = src.index(f"    def {name}(self")
    end = src.find("\n    def ", start + 10)
    return src[start: end if end != -1 else len(src)]


def _run():
    tests = [
        test_placement_comes_from_metadata_not_from_name,
        test_registry_hides_external_models,
        test_llm_lists_hide_external_models,
        test_discover_applies_policy_offline,
        test_filter_off_returns_all_models,
        test_connection_without_placement_signal_behaves_as_before,
        test_checkbox_disabled_without_placement_signal,
        test_lost_placement_signal_does_not_expand_list,
        test_migration_enables_filter_for_existing_connections,
        test_hidden_selected_model_is_blocked,
        test_safe_endpoint_requires_filter,
        test_hidden_count_is_logged_per_connection,
        test_model_type_replaces_name_guess,
        test_type_fallback_keeps_third_party_lists,
        test_type_filter_keeps_llm_out_of_dictation_list,
        test_connection_dialog_gates_checkboxes,
        test_llm_combo_hides_external_and_drops_hidden_current,
        test_registry_write_only_inside_checkpoint,
        test_set_cloud_models_cannot_bypass_filter,
        test_public_discover_signatures_unchanged,
    ]
    for t in tests:
        t()
        print(f"PASS: {t.__name__}")
    print("US-073 regression: ALL PASS")


if __name__ == "__main__":
    _run()
