"""US-073: где размещена облачная модель — внутри Cloud.ru или снаружи.

Заказчик (ИТ-директор) требует, чтобы сотрудник не мог выбрать модель, при
работе с которой данные уходят за пределы инфраструктуры Cloud.ru (152-ФЗ).
Признак размещения даёт сам сервис: в ответе `GET /v1/models` у каждой модели
есть `metadata.provider` — `cloud.ru` (внутренняя) либо `external` (внешняя).

Почему отдельный модуль:

* признак нужен и STT-списку (cloud_stt), и спискам LLM (cloud_llm), и реестру
  моделей (models) — держать разбор в одном месте дешевле, чем в трёх;
* модуль ЧИСТЫЙ (без Qt, без config, без сети), поэтому проверяется офлайн на
  фикстурах реального ответа сервиса.

ВАЖНО ПРО ИМЯ МОДЕЛИ: имя признаком не является и опираться на него нельзя.
Проверено на боевом ключе (TASK-363): `openai/gpt-oss-120b` — внутренняя,
`openai/gpt-oss-20b` — внешняя, `openai/whisper-large-v3` — внутренняя.
Адрес тоже не признак: у всех моделей один и тот же хост.

TASK-365: в том же `metadata` приходит `type` (`audio-to-text`, `llm`,
`embedder`, `rerank`, `guard`, `image+text-to-text`). Он заменяет угадывание
STT-моделей по имени — с обязательным откатом на разбор имени для сервисов,
которые тип не сообщают (иначе у сторонних OpenAI-совместимых провайдеров
список моделей диктовки опустел бы).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .logger import get_logger

log = get_logger("cloud_placement")

# Размещение модели.
PLACEMENT_INTERNAL = "internal"
PLACEMENT_EXTERNAL = "external"
PLACEMENT_UNKNOWN = ""  # сервис размещение не сообщил

# Значения metadata.provider, которые считаем внутренними. Список ЗАКРЫТЫЙ:
# незнакомое значение трактуется как «неизвестно», а не как «внутренняя» —
# для функции безопасности пропустить внешнюю модель хуже, чем скрыть лишнюю
# (AC 7: программа не открывает внешние модели молча).
INTERNAL_PROVIDER_VALUES = ("cloud.ru", "cloudru", "cloud_ru", "cloud-ru")
EXTERNAL_PROVIDER_VALUES = ("external",)

# TASK-365: значение metadata.type для моделей распознавания речи.
STT_MODEL_TYPE = "audio-to-text"


@dataclass
class CloudModelInfo:
    """Одна модель из ответа /v1/models с разобранными признаками."""

    id: str
    placement: str = PLACEMENT_UNKNOWN
    model_type: str = ""       # metadata.type как есть (в нижнем регистре)
    raw_provider: str = ""     # metadata.provider как есть (для журнала)


@dataclass
class EndpointSnapshot:
    """Последний разобранный ответ /v1/models одного эндпоинта (в памяти)."""

    placement: dict = field(default_factory=dict)  # model_id -> internal/external
    types: dict = field(default_factory=dict)      # model_id -> metadata.type
    reports_placement: bool = False                # хоть у одной модели был provider
    total: int = 0

    def internal_count(self) -> int:
        return sum(1 for v in self.placement.values() if v == PLACEMENT_INTERNAL)

    def external_count(self) -> int:
        return sum(1 for v in self.placement.values() if v == PLACEMENT_EXTERNAL)


# ---------- разбор ответа /v1/models ----------


def classify_provider(raw_provider) -> str:
    """metadata.provider → PLACEMENT_INTERNAL / PLACEMENT_EXTERNAL / PLACEMENT_UNKNOWN."""
    value = str(raw_provider or "").strip().lower()
    if not value:
        return PLACEMENT_UNKNOWN
    if value in EXTERNAL_PROVIDER_VALUES:
        return PLACEMENT_EXTERNAL
    if value in INTERNAL_PROVIDER_VALUES:
        return PLACEMENT_INTERNAL
    return PLACEMENT_UNKNOWN


def model_info_from_entry(entry) -> "CloudModelInfo | None":
    """Одна запись массива data ответа /v1/models → CloudModelInfo (или None)."""
    if not isinstance(entry, dict):
        return None
    model_id = str(entry.get("id") or "").strip()
    if not model_id:
        return None
    meta = entry.get("metadata")
    raw_provider = ""
    model_type = ""
    if isinstance(meta, dict):
        raw_provider = str(meta.get("provider") or "").strip()
        model_type = str(meta.get("type") or "").strip().lower()
    return CloudModelInfo(
        id=model_id,
        placement=classify_provider(raw_provider),
        model_type=model_type,
        raw_provider=raw_provider,
    )


def parse_models_payload(payload) -> "list[CloudModelInfo]":
    """Разобрать тело ответа /v1/models в список CloudModelInfo.

    Единая точка разбора для cloud_stt и cloud_llm: и признак размещения
    (US-073), и тип модели (TASK-365) читаются здесь, а не в двух местах.
    """
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    out: list[CloudModelInfo] = []
    for entry in data:
        info = model_info_from_entry(entry)
        if info is not None:
            out.append(info)
    return out


def snapshot_from_infos(infos) -> EndpointSnapshot:
    snap = EndpointSnapshot()
    for info in infos or []:
        snap.total += 1
        if info.placement:
            snap.placement[info.id] = info.placement
            snap.reports_placement = True
        if info.model_type:
            snap.types[info.id] = info.model_type
    return snap


# ---------- снимок эндпоинта и политика (в памяти) ----------

# Ключ — нормализованный base_url. Снимок наполняется ЛЮБЫМ discover-путём
# (STT, chat, полный список подключения), поэтому фильтр работает и там, где
# вызывающий код о размещении ничего не знает.
_snapshots: dict = {}
# Политика фильтра по эндпоинту: {"only_internal": bool, "reports": bool}.
# Публикуется из ModelManager.refresh_cloud_models — он единственный видит
# сразу все подключения (cfg) и вызывается при каждом изменении настроек.
_policies: dict = {}


def endpoint_key(base_url: str) -> str:
    """Нормализованный ключ эндпоинта (по смыслу — normalize_endpoint из
    cloud_security_dialog, но без зависимости от Qt-модуля)."""
    return (base_url or "").strip().rstrip("/").lower()


def elevenlabs_endpoint_key() -> str:
    """У ElevenLabs нет base_url в подключении — используем постоянный ключ."""
    return "elevenlabs"


def remember_endpoint_models(base_url: str, infos) -> EndpointSnapshot:
    """Запомнить разобранный ответ эндпоинта. Возвращает свежий снимок."""
    snap = snapshot_from_infos(infos)
    key = endpoint_key(base_url)
    _snapshots[key] = snap
    log.info(
        "placement: %s — моделей %d, с признаком размещения %d (внутренних %d, внешних %d), с типом %d",
        key or "(пусто)", snap.total, len(snap.placement),
        snap.internal_count(), snap.external_count(), len(snap.types),
    )
    return snap


def endpoint_snapshot(base_url: str) -> "EndpointSnapshot | None":
    return _snapshots.get(endpoint_key(base_url))


def forget_endpoints() -> None:
    """Сбросить снимки (вызывается вместе с invalidate_discover_cache)."""
    _snapshots.clear()


def set_endpoint_policy(base_url: str, *, only_internal: bool, reports: bool) -> None:
    _policies[endpoint_key(base_url)] = {
        "only_internal": bool(only_internal),
        "reports": bool(reports),
    }


def endpoint_policy(base_url: str) -> dict:
    return _policies.get(endpoint_key(base_url)) or {}


def clear_policies() -> None:
    _policies.clear()


# ---------- решение «показывать ли модель» ----------


def placement_of(model_id: str, *, stored_placement=None, base_url: str = "") -> str:
    """Размещение модели: сначала сохранённая в подключении карта, затем
    снимок последнего discover этого эндпоинта."""
    if isinstance(stored_placement, dict):
        value = stored_placement.get(model_id)
        if value:
            return str(value)
    snap = endpoint_snapshot(base_url)
    if snap is not None:
        value = snap.placement.get(model_id)
        if value:
            return str(value)
    return PLACEMENT_UNKNOWN


def type_of(model_id: str, *, stored_types=None, base_url: str = "") -> str:
    """TASK-365: тип модели (metadata.type) из подключения или из снимка."""
    if isinstance(stored_types, dict):
        value = stored_types.get(model_id)
        if value:
            return str(value).strip().lower()
    snap = endpoint_snapshot(base_url)
    if snap is not None:
        value = snap.types.get(model_id)
        if value:
            return str(value).strip().lower()
    return ""


def hidden_reason(
    model_id: str,
    *,
    only_internal: bool,
    reports_placement: bool,
    stored_placement=None,
    base_url: str = "",
) -> str:
    """Причина, по которой модель скрыта фильтром: "" — показывать.

    * ""         — модель разрешена (фильтр выключен, размещение внутреннее,
                   или сервис размещение вообще не сообщает);
    * "external" — сервис сказал, что модель внешняя;
    * "unknown"  — сервис размещение сообщает (или сообщал раньше), но про
                   ЭТУ модель признака нет. Скрываем: молча открывать внешние
                   модели нельзя (AC 7).
    """
    if not only_internal:
        return ""
    placement = placement_of(model_id, stored_placement=stored_placement, base_url=base_url)
    if placement == PLACEMENT_INTERNAL:
        return ""
    if placement == PLACEMENT_EXTERNAL:
        return "external"
    # Признака нет. Если эндпоинт его в принципе не даёт — фильтровать нечем,
    # список остаётся прежним (AC 5: для такого подключения флажок недоступен).
    snap = endpoint_snapshot(base_url)
    knows = bool(reports_placement) or bool(snap and snap.reports_placement)
    return "unknown" if knows else ""


def connection_hidden_reason(conn, model_id: str) -> str:
    """То же решение, но для объекта подключения (CloudConnection).

    Утиная типизация — чтобы модуль не зависел от config.py.
    """
    if conn is None:
        return ""
    base_url = getattr(conn, "base_url", "") or ""
    if not base_url and getattr(conn, "type", "") == "elevenlabs":
        base_url = elevenlabs_endpoint_key()
    return hidden_reason(
        model_id,
        only_internal=bool(getattr(conn, "only_internal_models", False)),
        reports_placement=bool(getattr(conn, "reports_model_placement", False)),
        stored_placement=getattr(conn, "model_placement", None),
        base_url=base_url,
    )


def connection_reports_placement(conn) -> bool:
    """Сообщает ли подключение размещение моделей (сохранённый признак или
    свежий снимок его эндпоинта). От этого зависит доступность флажка (AC 5)."""
    if conn is None:
        return False
    if getattr(conn, "reports_model_placement", False):
        return True
    base_url = getattr(conn, "base_url", "") or ""
    if not base_url and getattr(conn, "type", "") == "elevenlabs":
        base_url = elevenlabs_endpoint_key()
    snap = endpoint_snapshot(base_url)
    return bool(snap and snap.reports_placement)


def connection_model_type(conn, model_id: str) -> str:
    if conn is None:
        return ""
    base_url = getattr(conn, "base_url", "") or ""
    return type_of(model_id, stored_types=getattr(conn, "model_types", None), base_url=base_url)


def filter_connection_models(conn, ids) -> "tuple[list[str], dict]":
    """Отобрать модели подключения, разрешённые фильтром.

    Возвращает (разрешённые, {"external": n, "unknown": n}). Пустая статистика
    означает, что фильтр ничего не скрыл.
    """
    allowed: list[str] = []
    hidden = {"external": 0, "unknown": 0}
    for mid in ids or []:
        reason = connection_hidden_reason(conn, mid)
        if reason:
            hidden[reason] = hidden.get(reason, 0) + 1
        else:
            allowed.append(mid)
    return allowed, hidden


def filter_ids_by_policy(base_url: str, ids) -> "tuple[list[str], int]":
    """Отфильтровать список id по опубликованной политике эндпоинта.

    Используется discover-функциями, которые о подключении ничего не знают.
    Возвращает (разрешённые, сколько скрыто). Политики нет → список как есть.
    """
    policy = endpoint_policy(base_url)
    if not policy or not policy.get("only_internal"):
        return list(ids or []), 0
    allowed: list[str] = []
    hidden = 0
    for mid in ids or []:
        if hidden_reason(
            mid,
            only_internal=True,
            reports_placement=bool(policy.get("reports")),
            base_url=base_url,
        ):
            hidden += 1
        else:
            allowed.append(mid)
    if hidden:
        log.info("placement: %s — скрыто фильтром Cloud.ru %d моделей из %d",
                 endpoint_key(base_url) or "(пусто)", hidden, len(ids or []))
    return allowed, hidden
