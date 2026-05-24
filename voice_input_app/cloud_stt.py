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
from typing import Callable, Optional
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
) -> str:
    """POST {base_url}/audio/transcriptions с multipart/form-data.

    Совместимо с OpenAI Whisper API, Groq и любым OpenAI-compatible прокси.
    """
    if not api_key:
        raise CloudAuthError("API-ключ OpenAI-совместимого провайдера не задан")
    bad = _validate_api_key_charset(api_key)
    if bad:
        raise CloudAuthError(bad)
    url = base_url.rstrip("/") + "/audio/transcriptions"
    headers = {"Authorization": f"Bearer {api_key}"}
    data: dict[str, str] = {"model": model_id or "whisper-1", "response_format": "json"}
    if language:
        data["language"] = language
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
    return _parse_openai_response(response, url)


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
) -> str:
    """POST https://api.elevenlabs.io/v1/speech-to-text."""
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
    return _parse_openai_response(response, url)  # формат ответа идентичен — поле "text"


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


def split_and_transcribe(
    wav_path: Path,
    transcribe_fn: Callable[[Path], str],
    *,
    max_chunk_seconds: float = 60.0,
    overlap_seconds: float = 0.3,
    max_workers: int = 3,
    require_internet_host: Optional[str] = None,
    on_chunk_done: Optional[Callable[[int, int, float, float, str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    chunk_local_fallback: Optional[Callable[[Path], str]] = None,
) -> str:
    """Прозрачно расшифровывает длинную надиктовку через облачный API (US-032).

    Если файл короче max_chunk_seconds — отправляет одним запросом.
    Иначе нарезает на чанки и отправляет параллельно (до max_workers).
    Результаты склеиваются в порядке времени.

    require_internet_host — если задан, перед запросом делается проактивная
    проверка интернета (US-015 решение F). При недоступности — CloudNetworkError.

    on_chunk_done (TASK-078, US-017) — колбэк (index, total, start_sec, end_sec, text),
    вызывается по мере готовности каждого чанка. UI использует его, чтобы эмитить
    block_ready и обновлять прогресс файла прогрессивно (как при локальной модели).

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

    Любая ошибка любого чанка пробрасывается наверх (для триггера fallback
    на локальную модель в ModelManager.transcribe_with_fallback).
    """
    if require_internet_host and not is_internet_available(require_internet_host):
        raise CloudNetworkError(f"Нет соединения с интернетом (host={require_internet_host})")

    def _is_cancelled() -> bool:
        try:
            return bool(cancel_check and cancel_check())
        except Exception:  # noqa: BLE001
            return False

    duration = _wav_duration_seconds(wav_path)
    if duration <= 0:
        log.info("Cloud STT: duration unknown for %s, sending as single request", wav_path)
        if _is_cancelled():
            raise InterruptedError("Cloud transcription cancelled by user")
        result = transcribe_fn(wav_path)
        text = (result or "").strip()
        if on_chunk_done is not None and text:
            try:
                on_chunk_done(0, 1, 0.0, 0.0, text)
            except Exception:  # noqa: BLE001
                pass
        return text

    if duration <= max_chunk_seconds + 0.5:
        log.info("Cloud STT: short audio (%.1fs), single request", duration)
        if _is_cancelled():
            raise InterruptedError("Cloud transcription cancelled by user")
        result = transcribe_fn(wav_path)
        text = (result or "").strip()
        if on_chunk_done is not None and text:
            try:
                on_chunk_done(0, 1, 0.0, duration, text)
            except Exception:  # noqa: BLE001
                pass
        return text

    chunks = split_wav_by_duration(wav_path, chunk_seconds=max_chunk_seconds, overlap_seconds=overlap_seconds)
    if not chunks:
        if _is_cancelled():
            raise InterruptedError("Cloud transcription cancelled by user")
        result = transcribe_fn(wav_path)
        return (result or "").strip()

    total_chunks = len(chunks)
    log.info("Cloud STT: long audio (%.1fs) split into %d chunks", duration, total_chunks)

    results: list = [None] * total_chunks
    errors: list = []

    def _do_chunk(index: int, chunk_path: Path, start_sec: float, end_sec: float) -> None:
        if _is_cancelled():
            return
        # TASK-085: попытки cloud (с retry для 429), затем per-chunk local fallback
        last_exc: Optional[Exception] = None
        for attempt in range(2):  # 1-я попытка + 1 retry для rate limit
            try:
                chunk_text = transcribe_fn(chunk_path)
                text = (chunk_text or "").strip()
                results[index] = text
                if on_chunk_done is not None and not _is_cancelled():
                    try:
                        on_chunk_done(index, total_chunks, start_sec, end_sec, text)
                    except Exception:  # noqa: BLE001
                        log.exception("on_chunk_done callback failed for chunk %d", index)
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
        # TASK-085: попытка через chunk_local_fallback (per-chunk local recovery)
        if chunk_local_fallback is not None and not _is_cancelled():
            try:
                log.warning(
                    "Cloud chunk %d/%d failed (%s), recovering via local fallback…",
                    index + 1, total_chunks, type(last_exc).__name__ if last_exc else "unknown",
                )
                local_text = chunk_local_fallback(chunk_path)
                text = (local_text or "").strip()
                results[index] = text
                if on_chunk_done is not None and not _is_cancelled():
                    try:
                        on_chunk_done(index, total_chunks, start_sec, end_sec, text)
                    except Exception:  # noqa: BLE001
                        log.exception("on_chunk_done callback failed (local fallback) for chunk %d", index)
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

    parts = [r for r in results if r]
    return " ".join(parts).strip()
