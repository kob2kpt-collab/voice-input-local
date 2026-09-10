"""Машинные политики администратора (US-047, US-088).

ЕДИНСТВЕННАЯ точка чтения ветки реестра HKLM\\SOFTWARE\\Policies\\VoiceInputLocal.
Любая новая политика заводится здесь, а не отдельным winreg-вызовом в UI или
в облачных модулях: иначе смысл «политику нельзя обойти» теряется — достаточно
забыть одну проверку в новом пути кода (тот же довод, что у чокпоинта
регистрации облачных моделей в models._register_cloud_model, US-073).

Ветка Policies недоступна обычному пользователю на запись, поэтому политику
нельзя снять из-под учётной записи сотрудника. Раздаётся через GPO или
Kaspersky Security Center (примеры .reg — docs/enterprise-deploy.md).

Значения
--------
DisableUpdates            DWORD  ненулевое — встроенный апдейтер выключен (US-047)
DisableCloud              DWORD  ненулевое — облачные функции недоступны целиком
ForceInternalModelsOnly   DWORD  1 — фильтр «только модели Cloud.ru» принудительно
                                 включён, 0 — принудительно выключен, значения
                                 нет — решает пользователь
AllowedEndpoints          MULTI_SZ или SZ — белый список адресов подключений

Как трактуются сбои (AC 6 US-088)
---------------------------------
* Ветки политики нет — политики нет, поведение как сегодня. Это ОБЫЧНЫЙ
  случай (машина без централизованного управления), а не сбой.
* Значение есть, но прочитать или разобрать его не удалось — трактуем в самую
  строгую сторону и пишем предупреждение в журнал: администратор политику
  задал, и молча вернуться к «всё разрешено» нельзя.
* Исключение — DisableUpdates: у него сохранено прежнее поведение US-047
  (fail-open), потому что сбой чтения не должен «залипать» на отключённых
  обновлениях. Разница намеренная: не поставить обновление менее опасно, чем
  отправить рабочую запись в облако, которое администратор запретил.
"""
from __future__ import annotations

import os
from typing import Any

from .logger import get_logger

log = get_logger("policy")

POLICY_KEY = r"SOFTWARE\Policies\VoiceInputLocal"

VALUE_DISABLE_UPDATES = "DisableUpdates"
VALUE_DISABLE_CLOUD = "DisableCloud"
VALUE_FORCE_INTERNAL_MODELS_ONLY = "ForceInternalModelsOnly"
VALUE_ALLOWED_ENDPOINTS = "AllowedEndpoints"

# Состояния чтения одного значения.
STATE_MISSING = "missing"        # ветки или значения нет — политики нет
STATE_OK = "ok"                  # значение прочитано
STATE_UNREADABLE = "unreadable"  # значение есть, но не читается/не разбирается

# Постоянный адрес ElevenLabs: у этого подключения нет base_url, но белый
# список адресов должен уметь его разрешить или запретить наравне с прочими.
ELEVENLABS_ENDPOINT = "https://api.elevenlabs.io"


def _read_value(name: str) -> tuple[str, Any]:
    """Прочитать одно значение политики.

    Возвращает (состояние, значение). Состояние — STATE_MISSING / STATE_OK /
    STATE_UNREADABLE. Реестр читается заново при каждом обращении: политика
    меняется редко, а кэш в процессе означал бы, что применённая администратором
    политика ждёт перезапуска программы.
    """
    if os.name != "nt":
        return STATE_MISSING, None
    try:
        import winreg
    except Exception:  # noqa: BLE001 - winreg есть только на Windows
        return STATE_MISSING, None
    access = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, POLICY_KEY, 0, access)
    except FileNotFoundError:
        return STATE_MISSING, None
    except OSError:
        # Ветку открыть не удалось — узнать, есть ли в ней значения, нечем.
        # Считаем, что политики нет (как при отсутствии ветки), и пишем в
        # журнал: строгая трактовка здесь означала бы, что случайный сбой
        # реестра отключает облако у всех, включая машины без политики.
        log.warning("Ветка машинной политики недоступна", exc_info=True)
        return STATE_MISSING, None
    try:
        with key:
            value, _typ = winreg.QueryValueEx(key, name)
    except FileNotFoundError:
        return STATE_MISSING, None
    except OSError:
        log.warning("Значение политики %s есть, но не читается", name, exc_info=True)
        return STATE_UNREADABLE, None
    return STATE_OK, value


def _read_flag(name: str, *, strict_on_error: bool) -> bool | None:
    """Прочитать значение-флаг. None — значения нет (решает пользователь)."""
    state, value = _read_value(name)
    if state == STATE_MISSING:
        return None
    if state == STATE_UNREADABLE:
        log.warning(
            "Политика %s задана, но не читается — трактуем как %s",
            name, "включена" if strict_on_error else "выключена",
        )
        return bool(strict_on_error)
    try:
        return int(value) != 0
    except (TypeError, ValueError):
        log.warning(
            "Политика %s задана значением %r, которое не разбирается как число — трактуем как %s",
            name, value, "включена" if strict_on_error else "выключена",
        )
        return bool(strict_on_error)


# ---------- US-047: встроенные обновления ----------


def updates_disabled() -> bool:
    """Отключены ли встроенные обновления машинной политикой (US-047).

    Fail-open: нет ветки, нет значения, ошибка чтения или нечисловое значение
    означают «политики нет» — сбой чтения реестра не должен «залипать» на
    отключённых обновлениях.
    """
    return bool(_read_flag(VALUE_DISABLE_UPDATES, strict_on_error=False))


# ---------- US-088: работа с облаком ----------


def cloud_disabled() -> bool:
    """Запрещены ли облачные функции целиком (US-088, DisableCloud).

    Строгая трактовка сбоя: значение есть, но не читается — считаем, что
    облако запрещено. Локальные модели политика не затрагивает.
    """
    return bool(_read_flag(VALUE_DISABLE_CLOUD, strict_on_error=True))


def force_internal_models_only() -> bool | None:
    """Принудительное состояние фильтра «только модели Cloud.ru» (US-073).

    True  — фильтр включён принудительно, флажок в интерфейсе погашен;
    False — фильтр выключен принудительно, флажок погашен;
    None  — политики нет, решает пользователь (как сегодня).
    """
    return _read_flag(VALUE_FORCE_INTERNAL_MODELS_ONLY, strict_on_error=True)


def effective_only_internal_models(user_choice: bool) -> bool:
    """Действующее значение фильтра: политика важнее настройки пользователя."""
    forced = force_internal_models_only()
    if forced is None:
        return bool(user_choice)
    return bool(forced)


def _split_endpoints(value: Any) -> list[str]:
    """Разобрать значение AllowedEndpoints: MULTI_SZ или строка с разделителями."""
    if isinstance(value, (list, tuple)):
        raw_parts = [str(x) for x in value]
    else:
        text = str(value or "")
        for separator in ("\r", ";", ","):
            text = text.replace(separator, "\n")
        raw_parts = text.split("\n")
    return [p.strip() for p in raw_parts if p and p.strip()]


def allowed_endpoints() -> list[str] | None:
    """Белый список адресов подключений (US-088, AllowedEndpoints).

    None — политики нет, разрешены любые адреса. Пустой список — разрешено
    ничего: так трактуется и заданное, но неразбираемое значение (строгая
    сторона), и явно заданный пустой список.
    """
    state, value = _read_value(VALUE_ALLOWED_ENDPOINTS)
    if state == STATE_MISSING:
        return None
    if state == STATE_UNREADABLE:
        log.warning("Политика %s задана, но не читается — запрещаем все адреса", VALUE_ALLOWED_ENDPOINTS)
        return []
    try:
        return _split_endpoints(value)
    except Exception:  # noqa: BLE001
        log.warning("Политика %s не разбирается — запрещаем все адреса", VALUE_ALLOWED_ENDPOINTS, exc_info=True)
        return []


def _normalize_endpoint(value: str) -> str:
    """Привести адрес к виду host[:port]/path в нижнем регистре, без схемы.

    Сравнение вместе со схемой бессмысленно (администратор напишет адрес и с
    http, и без него), а по одному только хосту — слишком грубо: у Cloud.ru на
    одном хосте живут разные эндпоинты.
    """
    text = (value or "").strip().lower()
    for prefix in ("https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return text.strip("/")


def endpoint_allowed(base_url: str) -> bool:
    """Разрешён ли адрес подключения белым списком.

    Совпадением считается точное равенство нормализованных адресов или то, что
    адрес подключения лежит ВНУТРИ разрешённого (администратор написал хост —
    разрешены все его пути). Обратное неверно: разрешённый путь не открывает
    весь хост.
    """
    allowed = allowed_endpoints()
    if allowed is None:
        return True
    target = _normalize_endpoint(base_url)
    if not target:
        # Пустой адрес при заданном белом списке разрешить нельзя: неизвестно,
        # куда пойдёт запрос.
        return False
    for entry in allowed:
        norm = _normalize_endpoint(entry)
        if not norm:
            continue
        if target == norm or target.startswith(norm + "/"):
            return True
    return False


def connection_endpoint(conn) -> str:
    """Адрес подключения для проверки по белому списку (утиная типизация).

    У ElevenLabs нет base_url — подставляем постоянный адрес сервиса, иначе
    подключение к нему нельзя было бы ни разрешить, ни запретить осмысленно.
    """
    if conn is None:
        return ""
    base_url = (getattr(conn, "base_url", "") or "").strip()
    if not base_url and getattr(conn, "type", "") == "elevenlabs":
        return ELEVENLABS_ENDPOINT
    return base_url


def connection_allowed(conn) -> bool:
    """Разрешено ли подключение машинной политикой целиком."""
    if cloud_disabled():
        return False
    return endpoint_allowed(connection_endpoint(conn))


def connection_block_reason(conn) -> str:
    """Причина запрета подключения для показа пользователю. "" — разрешено."""
    if cloud_disabled():
        return "Облачные модели запрещены системным администратором."
    if not endpoint_allowed(connection_endpoint(conn)):
        endpoint = connection_endpoint(conn) or "(адрес не задан)"
        return (
            f"Адрес {endpoint} не входит в список разрешённых системным администратором. "
            "Подключение не используется."
        )
    return ""


def any_cloud_policy_active() -> bool:
    """Задана ли хоть одна политика по облаку (для пометок в интерфейсе)."""
    return bool(
        cloud_disabled()
        or force_internal_models_only() is not None
        or allowed_endpoints() is not None
    )


def describe() -> str:
    """Строка для журнала запуска: какие политики действуют на этой машине."""
    parts: list[str] = []
    if updates_disabled():
        parts.append("DisableUpdates=1")
    if cloud_disabled():
        parts.append("DisableCloud=1")
    forced = force_internal_models_only()
    if forced is not None:
        parts.append(f"ForceInternalModelsOnly={int(forced)}")
    allowed = allowed_endpoints()
    if allowed is not None:
        parts.append(f"AllowedEndpoints={len(allowed)} адрес(ов)")
    return ", ".join(parts) if parts else "нет"
