"""Cloud Speech-to-Text providers (US-015, US-016, US-032).

Поддерживаемые провайдеры:
- OpenAI-compatible API (OpenAI Whisper API, Groq и любой совместимый прокси)
- ElevenLabs Speech-to-Text

Модуль предоставляет:
- Низкоуровневые transcribe-функции для одного WAV-файла
- verify_*_connection — проверка ключа и доступности API
- discover_models — динамический список доступных STT-моделей у провайдера
- split_and_transcribe — нарезка длинной диктовки на чанки (US-032)
- is_internet_available — проактивная проверка сети перед запросом
"""

from __future__ import annotations

import socket
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional, Union
from urllib.parse import urlparse

import requests

from .audio_files import split_wav_by_duration
from .logger import get_logger

log = get_logger("cloud_stt")

# Типизированные исключения, чтобы вызывающий код мог различать причины
# (для UX-сообщений и решений о fallback).


class CloudSttError(Exception):
    """Базовое исключение для облачного STT."""


class CloudAuthError(CloudSttError):
    """401/403 — неверный или просроченный API-ключ."""


class CloudPayloadTooLarge(CloudSttError):
    """413 — файл превышает лимит провайдера."""


class CloudRateLimit(CloudSttError):
    """429 — превышен лимит запросов."""


class CloudServerError(CloudSttError):
    """5xx — ошибка на стороне провайдера."""


class CloudNetworkError(CloudSttError):
    """ConnectionError / Timeout / нет интернета."""


def _validate_api_key_charset(api_key: str) -> Optional[str]:
    """Проверка, что ключ кодируется в latin-1 (требование HTTP-заголовков).
    Если в ключ попали не-ASCII символы (кириллица, эмодзи и т.п.) — вернуть
    понятное сообщение для UI, чтобы не падать UnicodeEncodeError при запросе.
    Возвращает None если всё ок, иначе текст ошибки.
    """
    if not api_key:
        return None
    try:
        api_key.encode("latin-1")
    except UnicodeEncodeError:
        return ("API-ключ содержит недопустимые символы (кириллица или другие "
                "не-ASCII). Скопируйте ключ заново из личного кабинета провайдера.")
    return None


# Хосты провайдеров для проактивной проверки соединения
OPENAI_DEFAULT_HOST = "api.openai.com"
ELEVENLABS_HOST = "api.elevenlabs.io"
ELEVENLABS_BASE_URL = "https://api.elevenlabs.io/v1"

# Фильтр STT-моделей по подстрокам в id (US-015 решение E.1).
# Список расширен: у российских/корпоративных прокси модели могут называться
# нестандартно (saluteslp, yandex-stt, voice-to-text-v2 и т.п.).
STT_MODEL_ID_KEYWORDS = (
    "whisper", "transcribe", "scribe", "stt", "speech-to-text", "speech_to_text",
    "asr", "voice-to-text", "voice_to_text", "voicetotext",
    "audio", "speech", "recognition", "transcript",
)

# US-017 (TASK-057): модели OpenAI, которые НЕ поддерживают response_format=verbose_json
# (и, значит, не отдают segment-таймкоды). Для них при запросе with_timestamps мы шлём
# обычный json и возвращаем пустой список сегментов — текст сохраняется, таймкодов нет.
# gpt-4o-transcribe / gpt-4o-mini-transcribe / gpt-4o-transcribe-diarize поддерживают
# только response_format json|text. whisper-1 поддерживает verbose_json.
def _supports_verbose_json(model_id: str) -> bool:
    low = (model_id or "").lower()
    return "gpt-4o" not in low  # все gpt-4o-* transcribe-модели не умеют verbose_json


# Тип одного сегмента расшифровки с таймкодами (и опционально спикером).
# Используется в возврате transcribe_*(with_timestamps=True) и split_and_transcribe.
# Формат словаря: {"start": float, "end": float, "text": str, "speaker": str}
Segment = dict
TranscribeResult = Union[str, "tuple[str, list[Segment]]"]


# Whitelist используется ТОЛЬКО для ElevenLabs (у них нет /models endpoint для STT).
# Для OpenAI-compat — НЕТ whitelist: если discover не нашёл моделей, пользователь
# должен ввести id вручную через редактируемый combo. Это правильнее, чем подсовывать
# whisper-1, которой может не быть у провайдера (например, у российских прокси).
ELEVENLABS_STT_WHITELIST = ("scribe_v1", "scribe_v1_experimental")

# TASK-053 (US-017): лимиты размера файла у cloud-провайдеров для прямой
# отправки. Если исходный файл превышает лимит — UI показывает диалог с
# выбором между автонарезкой (split_and_transcribe) и переключением на
# локальную модель.
# Источники: docs.openai.com (25 MB для /v1/audio/transcriptions),
# elevenlabs.io/docs (для scribe_v1 — практический лимит ~1 GB на запрос).
PROVIDER_FILE_SIZE_LIMITS_MB = {
    "openai": 25,
    "elevenlabs": 1024,
}


def provider_file_size_limit_mb(provider: str) -> int | None:
    """Возвращает лимит размера файла в МБ для cloud-провайдера или None."""
    return PROVIDER_FILE_SIZE_LIMITS_MB.get((provider or "").lower())


# Таймауты (секунды)
CONNECT_TIMEOUT = 15  # увеличено: на медленных/корпоративных каналах TLS-handshake может занять >10с
# TASK-082 (US-017): READ_TIMEOUT уменьшен со 120 до 30 секунд.
# Для 60-сек WAV-чанка реальное время расшифровки обычно 5-15 секунд;
# 30 секунд — с большим запасом. Это сокращает максимальное время
# «висения» отмены cloud-расшифровки с 2 минут до 30 секунд в худшем
# случае (Python ThreadPoolExecutor не умеет прерывать запущенные
# потоки — приходится ждать естественного timeout/завершения).
READ_TIMEOUT = 30
VERIFY_READ_TIMEOUT = 30  # для verify_/discover — лёгкий GET, но даём запас для холодного DNS+TLS


# ---------- Internet availability ----------


def is_internet_available(host: str = OPENAI_DEFAULT_HOST, port: int = 443, timeout: float = 3.0) -> bool:
    """Проактивная проверка интернета (US-015 решение F).

    Делаем TCP-connect к указанному хосту/порту. Если соединение поднимается
    за timeout — считаем, что сеть доступна. Если падает (DNS-сбой,
    отсутствие маршрута, таймаут) — считаем, что сети нет.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.gaierror, socket.timeout):
        return False


def _host_from_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        return parsed.hostname or OPENAI_DEFAULT_HOST
    except Exception:  # noqa: BLE001
        return OPENAI_DEFAULT_HOST


def _wav_duration_seconds(wav_path: Path) -> float:
    try:
        with wave.open(str(wav_path), "rb") as src:
            frames = src.getnframes()
            framerate = src.getframerate()
            if framerate <= 0:
                return 0.0
            return frames / float(framerate)
    except Exception as exc:  # noqa: BLE001
        log.warning("Cannot read WAV duration %s: %s", wav_path, exc)
        return 0.0


# ---------- Низкоуровневые transcribe-функции (один файл) ----------


def transcribe_openai_compatible(
    wav_path: Path,
    api_key: str,
    base_url: str = "https://api.openai.com/v1",
    model_id: str = "whisper-1",
    language: Optional[str] = None,
    prompt: Optional[str] = None,
    with_timestamps: bool = False,
) -> "TranscribeResult":
    """POST {base_url}/audio/transcriptions с multipart/form-data.

    Совместимо с OpenAI Whisper API, Groq и любым OpenAI-compatible прокси.

    US-035: параметр `prompt` (Initial Prompt) — необязательная строка-пример
    стиля пунктуации/форматирования. Передаётся как обычное поле
    multipart/form-data. OpenAI Whisper учитывает только последние 224 токена.
    Не отправляется, если строка пустая или модель `gpt-4o-transcribe-diarize`
    (она не поддерживает параметр, OpenAI вернёт 400).

    US-017 (TASK-057): при `with_timestamps=True` запрашиваем
    `response_format=verbose_json` + `timestamp_granularities[]=segment` и
    возвращаем кортеж `(text, segments)`, где segment =
    `{"start": float, "end": float, "text": str, "speaker": ""}`.
    Сегменты verbose_json у whisper-1 — фразового уровня (≈предложение), а не
    по словам. Для моделей `gpt-4o*` (не умеют verbose_json) отправляем обычный
    json и возвращаем пустой список сегментов (текст сохраняется, таймкодов нет).
    При `with_timestamps=False` поведение прежнее — возврат `str`.
    """
    if not api_key:
        raise CloudAuthError("API-ключ OpenAI-совместимого провайдера не задан")
    bad = _validate_api_key_charset(api_key)
    if bad:
        raise CloudAuthError(bad)
    _model = model_id or "whisper-1"
    # US-017: verbose_json только если запрошены timestamps И модель его поддерживает.
    want_verbose = bool(with_timestamps) and _supports_verbose_json(_model)
    # US-035 diag: лог о том, сдан ли prompt и его длина/превью.
    # Помогает отлаживать «применился/не применился promt» без доступа к телу запроса.
    _prompt_stripped = (prompt or "").strip()
    _prompt_will_send = bool(_prompt_stripped) and _model != "gpt-4o-transcribe-diarize"
    _preview = _prompt_stripped[:60].replace("\n", " ")
    log.info(
        "openai prompt: will_send=%s chars=%d model=%s preview=%r timestamps=%s verbose=%s",
        _prompt_will_send, len(_prompt_stripped), _model, _preview, bool(with_timestamps), want_verbose,
    )
    url = base_url.rstrip("/") + "/audio/transcriptions"
    headers = {"Authorization": f"Bearer {api_key}"}
    # data как список кортежей — нужно для повторяющегося ключа timestamp_granularities[].
    data: list[tuple[str, str]] = [
        ("model", _model),
        ("response_format", "verbose_json" if want_verbose else "json"),
    ]
    if want_verbose:
        data.append(("timestamp_granularities[]", "segment"))
    if language:
        data.append(("language", language))
    # US-035: добавляем prompt, если задан и модель его поддерживает.
    if _prompt_stripped:
        unsupported_models = {"gpt-4o-transcribe-diarize"}
        if _model not in unsupported_models:
            data.append(("prompt", _prompt_stripped))
    try:
        with open(wav_path, "rb") as fh:
            files = {"file": (wav_path.name, fh, "audio/wav")}
            response = requests.post(
                url,
                headers=headers,
                data=data,
                files=files,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
    except requests.Timeout as exc:
        raise CloudNetworkError(f"Таймаут запроса к {url}") from exc
    except requests.ConnectionError as exc:
        raise CloudNetworkError(f"Ошибка соединения с {url}: {exc}") from exc
    if not with_timestamps:
        return _parse_openai_response(response, url)
    # US-017: вернуть (text, segments). Если verbose не запрашивали (gpt-4o*) —
    # сегментов нет, отдаём текст + пустой список.
    text = _parse_openai_response(response, url) if not want_verbose else None
    if not want_verbose:
        return (text or "", [])
    return _parse_openai_verbose_response(response, url)


def _parse_openai_verbose_response(response: "requests.Response", url: str) -> "tuple[str, list[Segment]]":
    """Разбор ответа response_format=verbose_json (US-017, TASK-057).

    Сначала прогоняем общую проверку статусов через _parse_openai_response, чтобы
    переиспользовать единые исключения (401/413/429/5xx). Затем повторно читаем
    JSON и извлекаем массив `segments` с полями start/end/text.
    """
    text = _parse_openai_response(response, url)  # бросит типизированное исключение при ошибке
    try:
        payload = response.json()
    except ValueError:
        return (text, [])
    segments: list[Segment] = []
    raw_segments = payload.get("segments") if isinstance(payload, dict) else None
    if isinstance(raw_segments, list):
        for seg in raw_segments:
            if not isinstance(seg, dict):
                continue
            seg_text = (seg.get("text") or "").strip()
            if not seg_text:
                continue
            try:
                start = float(seg.get("start") or 0.0)
                end = float(seg.get("end") or start)
            except (TypeError, ValueError):
                start, end = 0.0, 0.0
            segments.append({"start": start, "end": end, "text": seg_text, "speaker": ""})
    return (text, segments)


def _parse_openai_response(response: "requests.Response", url: str) -> str:
    if response.status_code == 401 or response.status_code == 403:
        raise CloudAuthError(f"Неверный API-ключ (HTTP {response.status_code})")
    if response.status_code == 413:
        raise CloudPayloadTooLarge(f"Файл превышает лимит API (HTTP 413) на {url}")
    if response.status_code == 429:
        raise CloudRateLimit(f"Превышен лимит запросов (HTTP 429) на {url}")
    if 500 <= response.status_code < 600:
        raise CloudServerError(f"Ошибка сервера {response.status_code} на {url}")
    if response.status_code != 200:
        text = response.text[:500] if response.text else ""
        raise CloudSttError(f"HTTP {response.status_code} на {url}: {text}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise CloudSttError(f"Ответ не является валидным JSON: {response.text[:200]}") from exc
    text = (payload.get("text") or "").strip()
    return text


def transcribe_elevenlabs(
    wav_path: Path,
    api_key: str,
    model_id: str = "scribe_v1",
    language: Optional[str] = None,
    with_timestamps: bool = False,
    with_diarization: bool = False,
) -> "TranscribeResult":
    """POST https://api.elevenlabs.io/v1/speech-to-text.

    US-017 (TASK-058): при `with_timestamps=True` ElevenLabs всегда отдаёт массив
    `words` со start/end (и speaker_id). Мы собираем из них фразовые сегменты и
    возвращаем `(text, segments)`. При `with_diarization=True` дополнительно шлём
    `diarize=true` и проставляем метки спикеров «Спикер N» (нативная диаризация
    ElevenLabs, в отличие от OpenAI, где она пост-процессится локально).
    При `with_timestamps=False` поведение прежнее — возврат `str`.

    Ограничение: при нарезке длинного файла на чанки ElevenLabs нумерует
    спикеров в каждом запросе независимо, поэтому метки между чанками могут не
    совпадать. Для коротких файлов (один запрос) нумерация консистентна.
    """
    if not api_key:
        raise CloudAuthError("API-ключ ElevenLabs не задан")
    bad = _validate_api_key_charset(api_key)
    if bad:
        raise CloudAuthError(bad)
    url = ELEVENLABS_BASE_URL + "/speech-to-text"
    headers = {"xi-api-key": api_key}
    data: dict[str, str] = {"model_id": model_id or "scribe_v1"}
    if language:
        # ElevenLabs принимает language_code (ISO 639-1)
        data["language_code"] = language
    if with_diarization:
        data["diarize"] = "true"
    try:
        with open(wav_path, "rb") as fh:
            files = {"file": (wav_path.name, fh, "audio/wav")}
            response = requests.post(
                url,
                headers=headers,
                data=data,
                files=files,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
    except requests.Timeout as exc:
        raise CloudNetworkError(f"Таймаут запроса к {url}") from exc
    except requests.ConnectionError as exc:
        raise CloudNetworkError(f"Ошибка соединения с {url}: {exc}") from exc
    if not with_timestamps:
        return _parse_openai_response(response, url)  # формат ответа: поле "text"
    return _parse_elevenlabs_verbose_response(response, url, with_diarization=with_diarization)


def _parse_elevenlabs_verbose_response(
    response: "requests.Response", url: str, *, with_diarization: bool
) -> "tuple[str, list[Segment]]":
    """Разбор ответа ElevenLabs STT с массивом `words` (US-017, TASK-058).

    Группируем подряд идущие слова в фразовые сегменты: новый сегмент начинается
    при смене speaker_id или после знака конца предложения (.!?…). Это даёт
    читаемые реплики вместо «по 1-2 слова на строку».
    """
    text = _parse_openai_response(response, url)  # единые исключения по статусу
    try:
        payload = response.json()
    except ValueError:
        return (text, [])
    words = payload.get("words") if isinstance(payload, dict) else None
    if not isinstance(words, list):
        return (text, [])

    # Карта speaker_id -> "Спикер N" в порядке появления.
    speaker_labels: dict[str, str] = {}

    def _label_for(sid: Optional[str]) -> str:
        if not with_diarization or not sid:
            return ""
        if sid not in speaker_labels:
            speaker_labels[sid] = f"Спикер {len(speaker_labels) + 1}"
        return speaker_labels[sid]

    segments: list[Segment] = []
    cur_words: list[str] = []
    cur_start: Optional[float] = None
    cur_end: float = 0.0
    cur_speaker: Optional[str] = None
    cur_label: str = ""

    def _flush() -> None:
        nonlocal cur_words, cur_start, cur_end, cur_speaker, cur_label
        if cur_words and cur_start is not None:
            seg_text = "".join(cur_words).strip()
            if seg_text:
                segments.append({
                    "start": float(cur_start),
                    "end": float(cur_end),
                    "text": seg_text,
                    "speaker": cur_label,
                })
        cur_words = []
        cur_start = None
        cur_end = 0.0
        cur_speaker = None
        cur_label = ""

    for w in words:
        if not isinstance(w, dict):
            continue
        wtype = (w.get("type") or "word").lower()
        wtext = w.get("text") or ""
        sid = w.get("speaker_id")
        if wtype == "spacing":
            # пробел/разделитель — приклеиваем к текущему сегменту, не создаём новый
            if cur_words:
                cur_words.append(wtext)
            continue
        try:
            w_start = float(w.get("start")) if w.get("start") is not None else None
            w_end = float(w.get("end")) if w.get("end") is not None else (w_start or 0.0)
        except (TypeError, ValueError):
            w_start, w_end = None, 0.0
        # Смена спикера → новый сегмент
        if cur_words and sid != cur_speaker:
            _flush()
        if not cur_words:
            cur_start = w_start if w_start is not None else 0.0
            cur_speaker = sid
            cur_label = _label_for(sid)
        cur_words.append(wtext)
        if w_end:
            cur_end = w_end
        elif w_start is not None:
            cur_end = w_start
        # Конец предложения → закрыть сегмент (фразовая гранулярность)
        if wtext.strip().endswith((".", "!", "?", "…")):
            _flush()
    _flush()
    return (text, segments)


# ---------- Verify connection ----------


def _log_proxy_env(url: str) -> None:
    """Логируем настроенные прокси из окружения — частая причина таймаутов
    в корпоративных сетях."""
    import os
    try:
        from requests.utils import get_environ_proxies
        env_proxies = get_environ_proxies(url)
    except Exception:  # noqa: BLE001
        env_proxies = {}
    env_keys = sorted(k for k in os.environ if "proxy" in k.lower())
    log.info("HTTP env: requests proxies for %s = %s; env vars: %s", url, env_proxies or "(none)", env_keys or "(none)")


def verify_openai_compatible_connection(api_key: str, base_url: str) -> tuple[bool, str]:
    """GET {base_url}/models — простой ping endpoint, есть у всех OpenAI-compat."""
    if not api_key:
        return False, "Заполните API Key"
    if not base_url:
        return False, "Заполните Base URL"
    bad = _validate_api_key_charset(api_key)
    if bad:
        log.warning("verify_openai: invalid charset in API key — aborting before HTTP")
        return False, bad
    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    _log_proxy_env(url)
    log.info("verify_openai: GET %s (timeouts=connect %ds / read %ds)", url, CONNECT_TIMEOUT, VERIFY_READ_TIMEOUT)
    started = time.monotonic()
    try:
        response = requests.get(url, headers=headers, timeout=(CONNECT_TIMEOUT, VERIFY_READ_TIMEOUT))
    except requests.Timeout as exc:
        elapsed = time.monotonic() - started
        log.warning("verify_openai: TIMEOUT after %.1fs on %s (%s)", elapsed, url, exc)
        return False, f"Таймаут соединения с {url} ({elapsed:.0f}с). Возможно — прокси/firewall."
    except requests.ConnectionError as exc:
        elapsed = time.monotonic() - started
        log.warning("verify_openai: ConnectionError after %.1fs on %s: %s", elapsed, url, exc)
        return False, f"Нет соединения с {_host_from_url(url)}: {exc}"
    elapsed = time.monotonic() - started
    log.info("verify_openai: HTTP %d in %.1fs", response.status_code, elapsed)
    if response.status_code == 200:
        return True, f"Соединение успешно ({elapsed:.1f}с)"
    if response.status_code in (401, 403):
        return False, f"Неверный API-ключ (HTTP {response.status_code})"
    return False, f"HTTP {response.status_code}: {response.text[:200]}"


def verify_elevenlabs_connection(api_key: str) -> tuple[bool, str]:
    """GET https://api.elevenlabs.io/v1/user — лёгкий endpoint для проверки ключа."""
    if not api_key:
        return False, "Заполните API Key"
    bad = _validate_api_key_charset(api_key)
    if bad:
        log.warning("verify_elevenlabs: invalid charset in API key — aborting before HTTP")
        return False, bad
    url = ELEVENLABS_BASE_URL + "/user"
    headers = {"xi-api-key": api_key}
    _log_proxy_env(url)
    log.info("verify_elevenlabs: GET %s (timeouts=connect %ds / read %ds)", url, CONNECT_TIMEOUT, VERIFY_READ_TIMEOUT)
    started = time.monotonic()
    try:
        response = requests.get(url, headers=headers, timeout=(CONNECT_TIMEOUT, VERIFY_READ_TIMEOUT))
    except requests.Timeout as exc:
        elapsed = time.monotonic() - started
        log.warning("verify_elevenlabs: TIMEOUT after %.1fs on %s (%s)", elapsed, url, exc)
        return False, f"Таймаут соединения с {url} ({elapsed:.0f}с). Возможно — прокси/firewall."
    except requests.ConnectionError as exc:
        elapsed = time.monotonic() - started
        log.warning("verify_elevenlabs: ConnectionError after %.1fs on %s: %s", elapsed, url, exc)
        return False, f"Нет соединения с {ELEVENLABS_HOST}: {exc}"
    elapsed = time.monotonic() - started
    log.info("verify_elevenlabs: HTTP %d in %.1fs", response.status_code, elapsed)
    if response.status_code == 200:
        return True, f"Соединение успешно ({elapsed:.1f}с)"
    if response.status_code in (401, 403):
        return False, f"Неверный API-ключ (HTTP {response.status_code})"
    return False, f"HTTP {response.status_code}: {response.text[:200]}"


# ---------- Discover models ----------

# In-memory cache: ключ provider+api_key+base_url → список model id
_discover_cache: dict[str, list[str]] = {}


def _cache_key(provider: str, api_key: str, base_url: Optional[str]) -> str:
    return f"{provider}|{api_key}|{base_url or ''}"


def invalidate_discover_cache() -> None:
    """Сбросить кэш discover (например, при смене ключа в настройках)."""
    _discover_cache.clear()


def discover_models(
    provider: str,
    api_key: str,
    base_url: Optional[str] = None,
    *,
    use_cache: bool = True,
) -> list[str]:
    """Запросить у провайдера список доступных STT-моделей.

    Возвращает отфильтрованный список model id (только STT-модели).
    При ошибке/недоступности или если фильтр ничего не нашёл —
    возвращает ПУСТОЙ список (а не whitelist). Это даёт пользователю
    честную картину: пустой combo → значит провайдер не вернул моделей
    или у них нестандартные имена; нужно ввести id вручную.
    Исключение — ElevenLabs: у них нет /models для STT, поэтому
    после успешного verify возвращаем whitelist (scribe_v1, scribe_v1_experimental).
    """
    if not api_key:
        return []
    ck = _cache_key(provider, api_key, base_url)
    if use_cache and ck in _discover_cache:
        return list(_discover_cache[ck])
    try:
        if provider == "openai":
            models = _discover_openai_compatible(api_key, base_url or "https://api.openai.com/v1")
        elif provider == "elevenlabs":
            models = _discover_elevenlabs(api_key)
        else:
            models = []
    except Exception as exc:  # noqa: BLE001
        log.warning("discover_models(%s) failed: %s", provider, exc)
        models = []
    _discover_cache[ck] = list(models)
    return models


def _looks_like_stt_model(model_id: str) -> bool:
    low = model_id.lower()
    return any(kw in low for kw in STT_MODEL_ID_KEYWORDS)


def _discover_openai_compatible(api_key: str, base_url: str) -> list[str]:
    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    log.info("discover_openai: GET %s", url)
    started = time.monotonic()
    try:
        response = requests.get(url, headers=headers, timeout=(CONNECT_TIMEOUT, VERIFY_READ_TIMEOUT))
    except requests.Timeout as exc:
        log.warning("discover_openai: TIMEOUT after %.1fs on %s (%s)", time.monotonic() - started, url, exc)
        return []
    except requests.ConnectionError as exc:
        log.warning("discover_openai: ConnectionError after %.1fs on %s: %s", time.monotonic() - started, url, exc)
        return []
    elapsed = time.monotonic() - started
    if response.status_code != 200:
        log.warning("discover_openai: HTTP %s in %.1fs on %s; body=%s",
                    response.status_code, elapsed, url, response.text[:300])
        return []
    try:
        payload = response.json()
    except ValueError as exc:
        log.warning("discover_openai: invalid JSON on %s (%s); body=%s", url, exc, response.text[:300])
        return []
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        log.warning("discover_openai: response has no 'data' list; payload keys=%s",
                    list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__)
        return []
    all_ids: list[str] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        model_id = str(entry.get("id") or "").strip()
        if model_id:
            all_ids.append(model_id)
    stt_ids = [mid for mid in all_ids if _looks_like_stt_model(mid)]
    stt_ids.sort()
    log.info("discover_openai: got %d total models, %d match STT filter (%s)",
             len(all_ids), len(stt_ids), ", ".join(STT_MODEL_ID_KEYWORDS))
    if all_ids and not stt_ids:
        # Логируем первые 30 ID, чтобы пользователь увидел, как реально называются
        # модели у его провайдера, и мог ввести нужный id вручную в combo.
        log.warning("discover_openai: STT filter matched 0. Available model ids (first 30): %s",
                    all_ids[:30])
    return stt_ids


def _discover_elevenlabs(api_key: str) -> list[str]:
    """ElevenLabs /v1/models возвращает TTS-модели. Для STT надёжнее использовать
    whitelist, но мы всё равно делаем verify-запрос /user, чтобы убедиться,
    что ключ валиден; если да — возвращаем whitelist."""
    ok, _msg = verify_elevenlabs_connection(api_key)
    if not ok:
        return []
    return list(ELEVENLABS_STT_WHITELIST)


# ---------- Высокоуровневая обёртка: split + parallel transcribe ----------


def _coerce_transcribe_result(
    result: "TranscribeResult",
    *,
    fallback_start: float,
    fallback_end: float,
) -> "tuple[str, list[Segment]]":
    """US-017 (TASK-059): нормализует возврат transcribe_fn к (text, segments).

    transcribe_fn в режиме with_timestamps возвращает (text, segments) с
    таймкодами ОТНОСИТЕЛЬНО чанка (0..длина_чанка). Если сегментов нет
    (например, модель gpt-4o*, не умеющая verbose_json) — синтезируем один
    сегмент на весь интервал [fallback_start..fallback_end], чтобы текст всё
    равно был привязан ко времени.
    """
    if isinstance(result, tuple):
        text, segs = result
        text = (text or "").strip()
        segs = list(segs or [])
    else:
        text = (result or "").strip()
        segs = []
    if not segs and text:
        segs = [{"start": 0.0, "end": max(0.0, fallback_end - fallback_start),
                 "text": text, "speaker": ""}]
    return text, segs


def _shift_segments(segs: "list[Segment]", offset: float) -> "list[Segment]":
    """Сдвигает start/end сегментов на абсолютный offset чанка (TASK-059)."""
    out: list[Segment] = []
    for s in segs:
        try:
            start = float(s.get("start", 0.0)) + offset
            end = float(s.get("end", 0.0)) + offset
        except (TypeError, ValueError):
            start, end = offset, offset
        seg_text = (s.get("text") or "").strip()
        if not seg_text:
            continue
        out.append({"start": start, "end": end, "text": seg_text, "speaker": s.get("speaker", "") or ""})
    return out


def split_and_transcribe(
    wav_path: Path,
    transcribe_fn: "Callable[[Path], TranscribeResult]",
    *,
    max_chunk_seconds: float = 60.0,
    overlap_seconds: float = 0.3,
    max_workers: int = 3,
    require_internet_host: Optional[str] = None,
    on_chunk_done: Optional[Callable[..., None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    chunk_local_fallback: Optional[Callable[[Path], str]] = None,
    with_timestamps: bool = False,
) -> "TranscribeResult":
    """Прозрачно расшифровывает длинную надиктовку через облачный API (US-032).

    Если файл короче max_chunk_seconds — отправляет одним запросом.
    Иначе нарезает на чанки и отправляет параллельно (до max_workers).
    Результаты склеиваются в порядке времени.

    require_internet_host — если задан, перед запросом делается проактивная
    проверка интернета (US-015 решение F). При недоступности — CloudNetworkError.

    on_chunk_done (TASK-078/TASK-059, US-017) — колбэк
    (index, total, start_sec, end_sec, text, segments), вызывается по мере
    готовности каждого чанка. `segments` — список абсолютных сегментов чанка
    (со сдвигом на offset чанка) при with_timestamps, иначе []. UI использует
    колбэк, чтобы эмитить block_ready прогрессивно (как при локальной модели).

    chunk_local_fallback (TASK-085, US-017) — колбэк (chunk_path) -> str.
    Если задан, при ошибке cloud-чанка (после retry на 429) этот чанк
    перерасшифровывается локальной моделью, а не пробрасывается ошибка
    наверх. Используется для расшифровки файлов, чтобы один сбойный чанк
    не приводил к перерасшифровке всего файла. Для диктовки колбэк не
    передаётся — там действует full fallback в transcribe_with_fallback.

    cancel_check (TASK-079, US-017) — колбэк () -> bool. Если возвращает True,
    функция перестаёт запускать новые чанки, отменяет ещё не запущенные
    в пуле через shutdown(cancel_futures=True), дожидается текущих in-flight
    и поднимает InterruptedError. Время отмены ≤ времени одного in-flight чанка
    (READ_TIMEOUT уменьшен до 30с в TASK-082).

    with_timestamps (TASK-059, US-017) — если True, transcribe_fn возвращает
    (text, segments), функция собирает абсолютные сегменты (сдвиг на offset
    чанка) и возвращает (full_text, merged_segments), отсортированные по start.
    При False поведение прежнее — возврат str.

    Любая ошибка любого чанка пробрасывается наверх (для триггера fallback
    на локальную модель в ModelManager.transcribe_with_fallback), кроме случая
    chunk_local_fallback (см. ниже).
    """
    if require_internet_host and not is_internet_available(require_internet_host):
        raise CloudNetworkError(f"Нет соединения с интернетом (host={require_internet_host})")

    def _is_cancelled() -> bool:
        try:
            return bool(cancel_check and cancel_check())
        except Exception:  # noqa: BLE001
            return False

    def _emit_chunk(index: int, total: int, start_sec: float, end_sec: float,
                    text: str, segs: "list[Segment]") -> None:
        if on_chunk_done is None or _is_cancelled():
            return
        try:
            on_chunk_done(index, total, start_sec, end_sec, text, segs)
        except Exception:  # noqa: BLE001
            log.exception("on_chunk_done callback failed for chunk %d", index)

    def _finish_single(text: str, segs: "list[Segment]") -> "TranscribeResult":
        return (text, segs) if with_timestamps else text

    duration = _wav_duration_seconds(wav_path)
    if duration <= 0:
        log.info("Cloud STT: duration unknown for %s, sending as single request", wav_path)
        if _is_cancelled():
            raise InterruptedError("Cloud transcription cancelled by user")
        text, segs = _coerce_transcribe_result(transcribe_fn(wav_path), fallback_start=0.0, fallback_end=0.0)
        if text:
            _emit_chunk(0, 1, 0.0, 0.0, text, segs)
        return _finish_single(text, segs)

    if duration <= max_chunk_seconds + 0.5:
        log.info("Cloud STT: short audio (%.1fs), single request", duration)
        if _is_cancelled():
            raise InterruptedError("Cloud transcription cancelled by user")
        text, segs = _coerce_transcribe_result(transcribe_fn(wav_path), fallback_start=0.0, fallback_end=duration)
        if text:
            _emit_chunk(0, 1, 0.0, duration, text, segs)
        return _finish_single(text, segs)

    chunks = split_wav_by_duration(wav_path, chunk_seconds=max_chunk_seconds, overlap_seconds=overlap_seconds)
    if not chunks:
        if _is_cancelled():
            raise InterruptedError("Cloud transcription cancelled by user")
        text, segs = _coerce_transcribe_result(transcribe_fn(wav_path), fallback_start=0.0, fallback_end=duration)
        return _finish_single(text, segs)

    total_chunks = len(chunks)
    log.info("Cloud STT: long audio (%.1fs) split into %d chunks", duration, total_chunks)

    # results[index] = (text, segments) — segments уже со сдвигом на offset чанка
    results: list = [None] * total_chunks
    errors: list = []

    def _store_and_emit(index: int, start_sec: float, end_sec: float, raw: "TranscribeResult") -> None:
        text, segs = _coerce_transcribe_result(raw, fallback_start=start_sec, fallback_end=end_sec)
        segs = _shift_segments(segs, start_sec) if with_timestamps else []
        results[index] = (text, segs)
        if not _is_cancelled():
            _emit_chunk(index, total_chunks, start_sec, end_sec, text, segs)

    def _do_chunk(index: int, chunk_path: Path, start_sec: float, end_sec: float) -> None:
        if _is_cancelled():
            return
        # TASK-085: попытки cloud (с retry для 429), затем per-chunk local fallback
        last_exc: Optional[Exception] = None
        for attempt in range(2):  # 1-я попытка + 1 retry для rate limit
            try:
                _store_and_emit(index, start_sec, end_sec, transcribe_fn(chunk_path))
                return
            except CloudRateLimit as exc:
                last_exc = exc
                if attempt == 0 and not _is_cancelled():
                    # 429 — backoff и retry один раз
                    log.warning("Cloud chunk %d/%d rate-limited, retrying in 3s…", index + 1, total_chunks)
                    time.sleep(3.0)
                    continue
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                break
        # Сюда попадаем только при ошибке cloud (retry не помог или другая ошибка)
        # TASK-085: попытка через chunk_local_fallback (per-chunk local recovery).
        # Локальный fallback возвращает str — _coerce синтезирует один сегмент на
        # весь интервал чанка, таймкоды сохраняются (with_timestamps).
        if chunk_local_fallback is not None and not _is_cancelled():
            try:
                log.warning(
                    "Cloud chunk %d/%d failed (%s), recovering via local fallback…",
                    index + 1, total_chunks, type(last_exc).__name__ if last_exc else "unknown",
                )
                _store_and_emit(index, start_sec, end_sec, chunk_local_fallback(chunk_path))
                log.info("Cloud chunk %d/%d recovered via local fallback", index + 1, total_chunks)
                return
            except Exception as local_exc:  # noqa: BLE001
                log.error("Local fallback for chunk %d also failed: %s", index, local_exc)
                # Записываем оригинальную cloud-ошибку, а не local — для UX логичнее
                errors.append(last_exc or local_exc)
                results[index] = None
                return
        # Нет chunk_local_fallback (например, для диктовки) — стандартное поведение
        if last_exc is not None:
            errors.append(last_exc)
        results[index] = None

    workers = max(1, min(max_workers, total_chunks))
    cancelled = False
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = []
        for i, ch in enumerate(chunks):
            start_sec = i * max(0.0, max_chunk_seconds - overlap_seconds)
            end_sec = min(duration, start_sec + max_chunk_seconds)
            futures.append(pool.submit(_do_chunk, i, ch.path, start_sec, end_sec))
        pending = set(futures)
        while pending:
            if _is_cancelled() and not cancelled:
                cancelled = True
                try:
                    pool.shutdown(wait=False, cancel_futures=True)
                except TypeError:
                    pool.shutdown(wait=False)
                log.info("Cloud STT: cancellation requested, draining in-flight chunks…")
            done_now = {f for f in pending if f.done()}
            pending -= done_now
            if pending:
                try:
                    next(iter(pending)).result(timeout=0.1)
                except Exception:  # noqa: BLE001
                    pass
    finally:
        if not cancelled:
            pool.shutdown(wait=False)

    for ch in chunks:
        try:
            ch.path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
    try:
        tmp_dir = chunks[0].path.parent
        if tmp_dir.exists() and not any(tmp_dir.iterdir()):
            tmp_dir.rmdir()
    except Exception:  # noqa: BLE001
        pass

    if cancelled:
        raise InterruptedError("Cloud transcription cancelled by user")

    # TASK-085: если был chunk_local_fallback, errors означают что и cloud, и
    # локальная попытка упали для этих чанков. Не пробрасываем ошибку — просто
    # склеиваем то, что удалось (включая локально восстановленные чанки).
    # Для диктовки (без chunk_local_fallback) — поведение прежнее: raise.
    if errors:
        if chunk_local_fallback is None:
            raise errors[0]
        # С chunk_local_fallback продолжаем — нерасшифрованные куски просто пропускаем
        log.warning(
            "Cloud transcription: %d chunks failed completely (cloud+local), skipping",
            len(errors),
        )

    # Склейка результатов в порядке чанков (results упорядочен по индексу).
    parts: list[str] = []
    all_segments: list[Segment] = []
    for r in results:
        if not r:
            continue
        t, segs = r
        if t:
            parts.append(t)
        if with_timestamps:
            all_segments.extend(segs)
    full_text = " ".join(parts).strip()
    if with_timestamps:
        all_segments.sort(key=lambda s: (s.get("start", 0.0), s.get("end", 0.0)))
        return (full_text, all_segments)
    return full_text
