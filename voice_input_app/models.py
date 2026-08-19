from __future__ import annotations

import gc
import os
import shutil
import tempfile
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

# GUI/windowed PyInstaller builds may have stdout/stderr set to None.
# Hugging Face/tqdm progress bars can crash in that environment, so all
# progress shown in the app is driven by our own directory-size monitor.
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("DISABLE_TQDM", "1")
from huggingface_hub import HfApi, hf_hub_download, snapshot_download
try:
    from huggingface_hub.utils import disable_progress_bars

    disable_progress_bars()
except Exception:
    pass

from . import cloud_placement
from .config import AppConfig
from .logger import get_logger
from .paths import models_dir
from .audio_files import format_duration, split_wav_by_duration

Progress = Callable[[object], None]
log = get_logger("models")
transcription_log = get_logger("transcription")
DEFAULT_MODEL_KEY = "whisper:small"

@dataclass(frozen=True)
class DownloadProgress:
    message: str
    percent: int | None = None
    downloaded_bytes: int = 0
    total_bytes: int = 0
    phase: str = "download"


@dataclass(frozen=True)
class TranscriptSegment:
    start_seconds: float
    end_seconds: float
    text: str
    speaker: str = ""


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if amount < 1024.0 or unit == "ТБ":
            if unit == "Б":
                return f"{int(amount)} {unit}"
            return f"{amount:.1f} {unit}"
        amount /= 1024.0
    return f"{int(value)} Б"


def _directory_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _resolve_hf_token(token: str | None = None) -> str | None:
    value = (token or os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN") or "").strip()
    return value or None


def _normalize_language(value: str | None) -> str | None:
    """Normalize UI/config language values to faster-whisper language codes."""
    raw = (value or "").strip()
    if not raw:
        return None
    lowered = raw.lower().replace("_", "-")
    aliases = {
        "auto": None,
        "авто": None,
        "automatic": None,
        "ru": "ru",
        "ru-ru": "ru",
        "russian": "ru",
        "русский": "ru",
        "en": "en",
        "en-us": "en",
        "en-gb": "en",
        "english": "en",
        "английский": "en",
    }
    if lowered in aliases:
        return aliases[lowered]
    if "-" in lowered:
        lowered = lowered.split("-", 1)[0]
    return lowered or None


def _repo_size_bytes(repo_id: str, token: str | None = None) -> int | None:
    try:
        info = HfApi(token=token).model_info(repo_id, files_metadata=True)
        total = 0
        for sibling in getattr(info, "siblings", []) or []:
            size = getattr(sibling, "size", None)
            if isinstance(size, int) and size > 0:
                total += size
        return total or None
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not get Hugging Face repo size for %s: %s", repo_id, exc)
        return None


def _single_file_size_bytes(repo_id: str, filename: str, token: str | None = None) -> int | None:
    """Get size of a single file in a HuggingFace repo (for GGUF downloads)."""
    try:
        info = HfApi(token=token).model_info(repo_id, files_metadata=True)
        for sibling in getattr(info, "siblings", []) or []:
            if getattr(sibling, "rfilename", None) == filename:
                size = getattr(sibling, "size", None)
                if isinstance(size, int) and size > 0:
                    return size
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not get file size for %s/%s: %s", repo_id, filename, exc)
        return None


def _emit_download_progress(
    progress: Optional[Progress],
    message: str,
    *,
    percent: int | None = None,
    downloaded_bytes: int = 0,
    total_bytes: int = 0,
    phase: str = "download",
) -> None:
    if progress is None:
        return
    try:
        progress(DownloadProgress(message=message, percent=percent, downloaded_bytes=downloaded_bytes, total_bytes=total_bytes, phase=phase))
    except TypeError:
        # Backwards-compatible fallback for older callbacks that expected text.
        progress(message)





def _ends_strongly(text: str) -> bool:
    return text.strip().endswith((".", "!", "?", "…"))


def merge_whisper_blocks_into_utterances(
    blocks: list[TranscriptSegment],
    *,
    max_gap_seconds: float = 1.15,
    max_duration_seconds: float = 28.0,
    max_chars: int = 420,
) -> list[TranscriptSegment]:
    """Merge short Whisper segments into user-friendly utterance-sized blocks.

    Whisper often emits 1-3 second segments, especially with timestamps. For
    file transcripts this is noisy, so we merge adjacent segments when there is
    no substantial pause and the combined block remains readable. Parakeet is
    intentionally not passed through this function because its native chunks are
    already closer to natural replies.
    """
    merged: list[TranscriptSegment] = []
    current: TranscriptSegment | None = None
    for block in blocks:
        text = block.text.strip()
        if not text:
            continue
        block = TranscriptSegment(block.start_seconds, block.end_seconds, text, block.speaker)
        if current is None:
            current = block
            continue
        gap = max(0.0, block.start_seconds - current.end_seconds)
        combined_text = (current.text.rstrip() + " " + block.text.lstrip()).strip()
        combined_duration = max(block.end_seconds, current.end_seconds) - current.start_seconds
        should_merge = (
            gap <= max_gap_seconds
            and combined_duration <= max_duration_seconds
            and len(combined_text) <= max_chars
            and not _ends_strongly(current.text)
        )
        # If the current block is very short, keep merging even after a comma or
        # soft punctuation so users do not see tiny two-second fragments.
        if not should_merge and len(current.text) < 120 and gap <= 0.55 and combined_duration <= max_duration_seconds:
            should_merge = True
        if should_merge:
            current = TranscriptSegment(current.start_seconds, max(current.end_seconds, block.end_seconds), combined_text, current.speaker or block.speaker)
        else:
            merged.append(current)
            current = block
    if current is not None:
        merged.append(current)
    return merged

def merge_speaker_segments(
    blocks: list[TranscriptSegment],
    *,
    max_gap_seconds: float = 2.0,
    max_duration_seconds: float = 60.0,
) -> list[TranscriptSegment]:
    """Merge consecutive segments that belong to the same speaker.

    Whisper with diarization often produces many 2-10 second fragments for a
    single speaker talking continuously.  This post-processing step combines
    adjacent same-speaker segments so the output is comparable to Parakeet's
    natural block sizes (BUG-02).
    """
    if not blocks:
        return blocks
    merged: list[TranscriptSegment] = []
    current = blocks[0]
    for block in blocks[1:]:
        same_speaker = (current.speaker and block.speaker and current.speaker == block.speaker)
        gap = max(0.0, block.start_seconds - current.end_seconds)
        combined_duration = block.end_seconds - current.start_seconds
        if same_speaker and gap <= max_gap_seconds and combined_duration <= max_duration_seconds:
            combined_text = current.text.rstrip() + " " + block.text.lstrip()
            current = TranscriptSegment(
                current.start_seconds,
                max(current.end_seconds, block.end_seconds),
                combined_text.strip(),
                current.speaker,
            )
        else:
            merged.append(current)
            current = block
    merged.append(current)
    return merged


@dataclass(frozen=True)
class ModelSpec:
    key: str
    engine: str
    name: str
    repo_id: str
    loader_name: str
    language_hint: str
    size_hint: str
    note: str


WHISPER_MODELS: dict[str, ModelSpec] = {
    "whisper:tiny": ModelSpec("whisper:tiny", "Whisper", "Tiny", "Systran/faster-whisper-tiny", "tiny", "мультиязычная / русский", "~75 МБ", "OpenAI-origin CT2; самая быстрая, точность ниже"),
    "whisper:base": ModelSpec("whisper:base", "Whisper", "Base", "Systran/faster-whisper-base", "base", "мультиязычная / русский", "~145 МБ", "OpenAI-origin CT2; быстрый режим"),
    "whisper:small": ModelSpec("whisper:small", "Whisper", "Small", "Systran/faster-whisper-small", "small", "мультиязычная / русский", "~485 МБ", "OpenAI-origin CT2; рекомендуется по умолчанию"),
    "whisper:medium": ModelSpec("whisper:medium", "Whisper", "Medium", "Systran/faster-whisper-medium", "medium", "мультиязычная / русский", "~1.5 ГБ", "OpenAI-origin CT2; выше точность"),
    "whisper:large-v3": ModelSpec("whisper:large-v3", "Whisper", "Large v3", "Systran/faster-whisper-large-v3", "large-v3", "мультиязычная / русский", "~3 ГБ", "OpenAI-origin CT2; максимальное качество Whisper"),
    "whisper:large-v3-turbo": ModelSpec("whisper:large-v3-turbo", "Whisper", "Large v3 Turbo", "h2oai/faster-whisper-large-v3-turbo", "h2oai/faster-whisper-large-v3-turbo", "мультиязычная / русский", "~1.6 ГБ", "OpenAI Large v3 Turbo в формате CTranslate2; быстрее Large v3"),
}

PARAKEET_MODELS: dict[str, ModelSpec] = {
    "parakeet:v2": ModelSpec("parakeet:v2", "Parakeet", "TDT 0.6B v2 ONNX", "istupakov/parakeet-tdt-0.6b-v2-onnx", "nemo-parakeet-tdt-0.6b-v2", "английская", "~1.5-2.5 ГБ", "Английская, ONNX Runtime"),
    "parakeet:v3": ModelSpec("parakeet:v3", "Parakeet", "TDT 0.6B v3 ONNX", "istupakov/parakeet-tdt-0.6b-v3-onnx", "nemo-parakeet-tdt-0.6b-v3", "25 языков", "~1.5-2.5 ГБ", "Мультиязычная, ONNX Runtime"),
}

ADDITIONAL_MODELS: dict[str, ModelSpec] = {
    "addon:vad": ModelSpec("addon:vad", "Дополнительно", "VAD для точных таймкодов", "ggml-org/whisper-vad", "ggml-silero-v6.2.0.bin", "служебная", "~1 МБ", "Дополнительная модель для более стабильной разбивки файла на фразы"),
    "addon:sortformer": ModelSpec("addon:sortformer", "Дополнительно", "Sortformer Diarization v2.1", "cgus/diar_streaming_sortformer_4spk-v2.1-onnx", "diar_streaming_sortformer_4spk-v2.1.onnx", "до 4 спикеров", "~492 МБ", "ONNX-модель для определения говорящих в файлах; функция выключена по умолчанию"),
}

SUMMARY_MODELS: dict[str, ModelSpec] = {
    "summary:qwen3-1.7b": ModelSpec(
        "summary:qwen3-1.7b", "Суммаризация", "Qwen3 1.7B Q4_K_M",
        "bartowski/Qwen_Qwen3-1.7B-GGUF",
        "Qwen_Qwen3-1.7B-Q4_K_M.gguf",
        "мультиязычная / русский",
        "~1.2 ГБ",
        "GGUF-модель для локальной суммаризации расшифровок; CPU-friendly Q4"
    ),
}
DEFAULT_SUMMARY_MODEL_KEY = "summary:qwen3-1.7b"

TRANSCRIPTION_MODELS: dict[str, ModelSpec] = {**WHISPER_MODELS, **PARAKEET_MODELS}
ALL_MODELS: dict[str, ModelSpec] = {**TRANSCRIPTION_MODELS, **ADDITIONAL_MODELS, **SUMMARY_MODELS}

# US-015, US-016: Cloud STT models registry.
# Заполняется динамически через ModelManager.refresh_cloud_models(cfg)
# на основе настроек API-ключей и discover_models() от провайдера.
# Ключ формата "cloud:<provider>:<model_id>", например "cloud:openai:whisper-1".
_CLOUD_MODELS_REGISTRY: dict[str, ModelSpec] = {}
CLOUD_OPENAI_ENGINE = "Cloud-OpenAI"
CLOUD_ELEVENLABS_ENGINE = "Cloud-ElevenLabs"


def is_cloud_model_key(key: str) -> bool:
    return key.startswith("cloud:")


def cloud_provider_of(key: str) -> str:
    """Извлечь имя провайдера из cloud-ключа: cloud:openai:xxx → "openai"."""
    parts = key.split(":", 2)
    if len(parts) >= 2 and parts[0] == "cloud":
        return parts[1]
    return ""


def cloud_model_id_of(key: str) -> str:
    """Извлечь model_id из cloud-ключа: cloud:<connection_id>:whisper-1 → "whisper-1"."""
    parts = key.split(":", 2)
    if len(parts) >= 3 and parts[0] == "cloud":
        return parts[2]
    return ""


def cloud_connection_id_of(key: str) -> str:
    """US-037: средний сегмент cloud-ключа — id подключения.
    cloud:<connection_id>:<model_id> → <connection_id>. Совпадает со средним
    сегментом, который раньше был именем провайдера (cloud_provider_of)."""
    return cloud_provider_of(key)


def resolve_cloud_connection(cfg, key: str):
    """US-037: вернуть CloudConnection для cloud-ключа или None (подключение
    удалено / cfg отсутствует). Вызывающий обязан обработать None."""
    if cfg is None:
        return None
    try:
        return cfg.connection_by_id(cloud_connection_id_of(key))
    except Exception:  # noqa: BLE001
        return None


def _make_cloud_spec(engine: str, display_provider: str, model_id: str) -> ModelSpec:
    key = f"cloud:{display_provider.lower()}:{model_id}" if False else None  # placeholder unused
    # На самом деле ключ строится в refresh_cloud_models — здесь только spec поля
    if engine == CLOUD_OPENAI_ENGINE:
        name = f"OpenAI — {model_id}"
        note = "OpenAI-совместимый STT API"
    elif engine == CLOUD_ELEVENLABS_ENGINE:
        name = f"ElevenLabs — {model_id}"
        note = "ElevenLabs Speech-to-Text"
    else:
        name = model_id
        note = "Cloud STT"
    return ModelSpec(
        key="",  # будет задано вызывающим
        engine=engine,
        name=name,
        repo_id="",
        loader_name=model_id,
        language_hint="мультиязычная",
        size_hint="облако",
        note=note,
    )
# US-050: единый ГИБКИЙ набор обязательных файлов для ВСЕХ моделей Whisper.
# faster-whisper/CTranslate2 для загрузки нужны model.bin + config.json + tokenizer.json
# и словарь. Репозитории Systran разнятся: у tiny/base/small/medium словарь называется
# vocabulary.txt и НЕТ preprocessor_config.json; у large-v3/turbo — vocabulary.json
# (+ preprocessor_config.json, который для загрузки НЕ требуется). Поэтому требуем базовые
# файлы + словарь в ЛЮБОМ из двух форматов, а preprocessor_config.json НЕ требуем (если он
# есть в репозитории — скачается сам вместе с остальными, проверку это не ломает).
WHISPER_REQUIRED_FILES = ("model.bin", "config.json", "tokenizer.json")
WHISPER_VOCAB_ALTERNATIVES = ("vocabulary.json", "vocabulary.txt")
PARAKEET_MODEL_SUFFIXES = (".onnx", ".json", ".yaml", ".yml")
ADDITIONAL_REQUIRED_FILES_BY_KEY: dict[str, tuple[str, ...]] = {
    "addon:vad": ("ggml-silero-v6.2.0.bin",),
    "addon:sortformer": ("diar_streaming_sortformer_4spk-v2.1.onnx",),
}


def model_display_name(key: str) -> str:
    spec = ALL_MODELS.get(key)
    if spec is None:
        spec = _CLOUD_MODELS_REGISTRY.get(key)
    if spec is None:
        # TASK-056 (US-017): для cloud-ключей, которых нет ни в ALL_MODELS,
        # ни в _CLOUD_MODELS_REGISTRY (ключ удалён из настроек, история
        # помнит расшифровку), собираем читабельное имя из самого ключа.
        if is_cloud_model_key(key):
            provider = cloud_provider_of(key)
            model_id = cloud_model_id_of(key)
            provider_label = {"openai": "OpenAI", "elevenlabs": "ElevenLabs"}.get(provider, provider or "Cloud")
            return f"{provider_label} · {model_id}"
        return key
    # Для cloud-моделей spec.name уже содержит провайдера ("OpenAI — whisper-1"),
    # дублировать engine не нужно
    if is_cloud_model_key(key):
        return spec.name
    return f"{spec.engine} — {spec.name}"


def _has_incomplete_downloads(path: Path) -> bool:
    if not path.exists():
        return False
    return any(p.name.endswith(".incomplete") for p in path.rglob("*"))


def _is_summary_model(spec: ModelSpec) -> bool:
    """Summary models are single GGUF files, not directories."""
    return spec.key in SUMMARY_MODELS


def _missing_required_files(spec: ModelSpec, path: Path) -> list[str]:
    missing: list[str] = []
    # Summary GGUF models are single files, not directories
    if _is_summary_model(spec):
        gguf_path = path / spec.loader_name
        if not gguf_path.is_file() or gguf_path.stat().st_size < 1_000_000:
            missing.append(spec.loader_name)
        return missing
    if not path.exists() or not path.is_dir():
        return ["<model directory>"]
    if _has_incomplete_downloads(path):
        missing.append("*.incomplete")
    if spec.engine == "Whisper":
        for name in WHISPER_REQUIRED_FILES:
            file_path = path / name
            if not file_path.is_file() or file_path.stat().st_size <= 0:
                missing.append(name)
        # Словарь может называться vocabulary.json (large-v3/turbo) или vocabulary.txt
        # (tiny/base/small/medium) — принимаем ЛЮБОЙ из форматов.
        has_vocab = any((path / name).is_file() and (path / name).stat().st_size > 0 for name in WHISPER_VOCAB_ALTERNATIVES)
        if not has_vocab:
            missing.append("vocabulary.json or vocabulary.txt")
    elif spec.engine == "Parakeet":
        has_model_file = any(p.is_file() and p.suffix.lower() in PARAKEET_MODEL_SUFFIXES and p.stat().st_size > 0 for p in path.rglob("*"))
        if not has_model_file:
            missing.append("*.onnx or model metadata")
    elif spec.engine == "Дополнительно":
        for name in ADDITIONAL_REQUIRED_FILES_BY_KEY.get(spec.key, (spec.loader_name,)):
            file_path = path / name
            if not file_path.is_file() or file_path.stat().st_size <= 0:
                missing.append(name)
    elif not any(path.iterdir()):
        missing.append("<any model file>")
    return missing


def _is_complete_model_dir(spec: ModelSpec, path: Path) -> bool:
    return not _missing_required_files(spec, path)


def _looks_like_corrupt_model_error(exc: BaseException) -> bool:
    """US-052: похоже ли исключение загрузки на битый/неоткрываемый файл модели.

    Ошибка CTranslate2 при недокачанном/повреждённом model.bin —
    'Unable to open file ...model.bin...'. Реагируем только на такие случаи,
    чтобы не перекачивать модель из-за посторонних ошибок.
    """
    msg = str(exc).lower()
    return (
        "unable to open file" in msg
        or "model.bin" in msg
        or "unable to load" in msg
        or "invalid model" in msg
    )


def _purge_hf_cache_for_repo(repo_id: str) -> bool:
    """US-052: удалить папку кэша HuggingFace для репозитория. True — если что-то удалено.

    faster-whisper сверяет только etag и переиспользует битый локальный model.bin.
    Чтобы перекачать начисто, удаляем каталог кэша репозитория целиком.
    Защита: удаляем только каталог с именем вида 'models--<org>--<name>'.
    """
    candidates: list[Path] = []
    # 1) Точный путь через try_to_load_from_cache: .../models--X--Y/snapshots/<hash>/<file>
    try:
        from huggingface_hub import try_to_load_from_cache

        for fname in ("model.bin", "config.json"):
            cached = try_to_load_from_cache(repo_id, fname)
            if isinstance(cached, str):
                candidates.append(Path(cached).parents[2])
                break
    except Exception:  # noqa: BLE001 — best-effort, ниже есть fallback
        pass
    # 2) Fallback по соглашению об именовании кэша HuggingFace
    try:
        from huggingface_hub.constants import HF_HUB_CACHE

        candidates.append(Path(HF_HUB_CACHE) / ("models--" + repo_id.replace("/", "--")))
    except Exception:  # noqa: BLE001
        pass

    removed = False
    seen: set[str] = set()
    for repo_dir in candidates:
        key = str(repo_dir)
        if key in seen:
            continue
        seen.add(key)
        try:
            if repo_dir.exists() and repo_dir.name.startswith("models--"):
                shutil.rmtree(repo_dir, ignore_errors=True)
                removed = True
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось очистить кэш HuggingFace %s: %s", repo_dir, exc)
    return removed


def _whisper_model_cached(repo_id: str) -> bool:
    """US-051: есть ли в кэше HuggingFace скачанный (непустой) model.bin для репозитория.

    Дешёвая проверка без сети: try_to_load_from_cache возвращает путь только для
    полностью закачанного файла (huggingface_hub сверяет etag при загрузке), поэтому
    оборванная загрузка (нет закоммиченного blob) вернёт None. Проверяем существование
    и ненулевой размер. Редкий случай «файл есть, etag совпал, но CTranslate2 не
    открывает» ловит self-heal при загрузке (US-052) — статусу его дёшево не увидеть.
    """
    try:
        from huggingface_hub import try_to_load_from_cache

        cached = try_to_load_from_cache(repo_id, "model.bin")
        if isinstance(cached, str):
            p = Path(cached)
            return p.is_file() and p.stat().st_size > 0
    except Exception:  # noqa: BLE001
        pass
    return False


def _safe_rmtree(path: Path, *, retries: int = 3) -> None:
    if not path.exists():
        return
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            shutil.rmtree(path)
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            gc.collect()
            time.sleep(0.35 * (attempt + 1))
    raise RuntimeError(
        "Не удалось удалить папку модели. Скорее всего, файл занят другим процессом "
        "или незавершённой загрузкой. Закройте Voice Input Local, подождите несколько секунд "
        "и попробуйте снова. Исходная ошибка: " + str(last_error)
    )


class ModelManager:
    def __init__(self) -> None:
        self._loaded: dict[str, object] = {}
        self._locks: dict[str, threading.RLock] = {}
        # Последний применённый AppConfig — нужен для проверки доступности
        # cloud-моделей (наличие API-ключа в cfg). Заполняется refresh_cloud_models().
        self._last_cfg: AppConfig | None = None

    def _lock_for(self, key: str) -> threading.RLock:
        if key not in self._locks:
            self._locks[key] = threading.RLock()
        return self._locks[key]

    def spec(self, key: str) -> ModelSpec:
        if key in ALL_MODELS:
            return ALL_MODELS[key]
        if key in _CLOUD_MODELS_REGISTRY:
            return _CLOUD_MODELS_REGISTRY[key]
        raise KeyError(f"Неизвестная модель: {key}")

    # Cyrillic engine names → ASCII-safe folder names for filesystem
    _ENGINE_DIRS = {"Суммаризация": "summarization"}

    def model_path(self, key: str) -> Path:
        if is_cloud_model_key(key):
            raise ValueError(f"Cloud-модель не имеет локального пути: {key}")
        spec = self.spec(key)
        safe = key.replace(":", "_")
        engine_dir = self._ENGINE_DIRS.get(spec.engine, spec.engine.lower())
        return models_dir() / engine_dir / safe

    def is_cloud_model(self, key: str) -> bool:
        return is_cloud_model_key(key)

    def cloud_model_keys(self) -> list[str]:
        """Все зарегистрированные cloud-модели (для UI dropdown, включая «не настроено»)."""
        return list(_CLOUD_MODELS_REGISTRY.keys())

    def refresh_cloud_models(self, cfg: AppConfig) -> None:
        """Перестроить реестр cloud-моделей на основе сохранённого AppConfig.

        ВАЖНО: метод НЕ делает HTTP-запросов. Регистрирует ТОЛЬКО те модели,
        которые пользователь уже один раз успешно обнаружил через кнопку
        «Проверить соединение» (они сохранены в cfg.openai_stt_model_id /
        cfg.elevenlabs_stt_model_id). До этого момента cloud-моделей в списке
        нет (US-021: не показываем заглушки до подтверждения провайдером).

        После успешного verify в UI вызывается set_cloud_models() — он
        регистрирует все обнаруженные модели напрямую из ответа воркера.
        """
        # US-073: присваивание ПЕРВЫМ оператором обязательно. Дальше метод
        # разбирает подключения из config.json (файл правят руками), и
        # исключение в середине не должно оставить менеджер без конфига:
        # без него чокпоинт _register_cloud_model отвергает ВСЁ (fail-closed),
        # и облачные модели просто исчезли бы из списков.
        self._last_cfg = cfg
        _CLOUD_MODELS_REGISTRY.clear()

        # US-037: реестр строится из подключений и их кэша discovered_models
        # (без HTTP). Ключи — cloud:<connection_id>:<model_id>.
        # Отбор STT-моделей и фильтр размещения (US-073) выполняет
        # _register_cloud_model — единая точка регистрации.
        connections = list(getattr(cfg, "cloud_connections", None) or [])
        # US-073: публикуем политику фильтра по эндпоинтам. Это нужно
        # discover-функциям (cloud_stt/cloud_llm), которые о подключениях
        # ничего не знают: даже промежуточные списки не расширятся внешними
        # моделями. Перепубликация полная — чтобы удалённое подключение не
        # оставляло за собой политику.
        cloud_placement.clear_policies()
        for conn in connections:
            base = getattr(conn, "base_url", "") or ""
            if not base and getattr(conn, "type", "") == "elevenlabs":
                base = cloud_placement.elevenlabs_endpoint_key()
            cloud_placement.set_endpoint_policy(
                base,
                only_internal=bool(getattr(conn, "only_internal_models", False)),
                reports=cloud_placement.connection_reports_placement(conn),
            )
        for conn in connections:
            registered = 0
            skipped: dict[str, int] = {}
            for mid in (getattr(conn, "discovered_models", None) or []):
                reason = self._register_cloud_model(conn.id, conn.type, mid)
                if reason:
                    skipped[reason] = skipped.get(reason, 0) + 1
                else:
                    registered += 1
            self._log_cloud_registration(conn, registered, skipped)

    def set_cloud_models(self, a, b, c=None) -> None:
        """US-037: зарегистрировать обнаруженные модели ОДНОГО подключения.

        Новый вызов: set_cloud_models(connection_id, conn_type, model_ids).
        Обратная совместимость (старый UI до рефакторинга US-037):
        set_cloud_models(provider, model_ids) — провайдер маппится на первое
        подключение этого типа. Не делает HTTP — model_ids получены воркером.
        """
        if c is None:
            # Старый стиль: (provider, model_ids)
            provider, model_ids = a, list(b or [])
            conn = None
            cfg = self._last_cfg
            if cfg is not None:
                for cc in (getattr(cfg, "cloud_connections", None) or []):
                    if cc.type == provider:
                        conn = cc
                        break
            if conn is None:
                return
            connection_id, conn_type = conn.id, conn.type
        else:
            connection_id, conn_type, model_ids = a, b, list(c or [])
        for key in list(_CLOUD_MODELS_REGISTRY.keys()):
            if cloud_connection_id_of(key) == connection_id:
                _CLOUD_MODELS_REGISTRY.pop(key, None)
        # US-073: регистрация идёт через тот же чокпоинт, поэтому список,
        # пришедший из проверки соединения, тоже не может расширить реестр
        # внешними моделями.
        registered = 0
        skipped: dict[str, int] = {}
        for mid in model_ids:
            reason = self._register_cloud_model(connection_id, conn_type, mid)
            if reason:
                skipped[reason] = skipped.get(reason, 0) + 1
            else:
                registered += 1
        conn_obj = None
        if self._last_cfg is not None:
            try:
                conn_obj = self._last_cfg.connection_by_id(connection_id)
            except Exception:  # noqa: BLE001
                conn_obj = None
        if conn_obj is not None:
            self._log_cloud_registration(conn_obj, registered, skipped)
        else:
            # US-073: подключение не найдено в конфиге — чокпоинт отверг всё
            # (fail-closed). Это предупреждение, а не информация: списки
            # облачных моделей в таком состоянии окажутся пустыми.
            log.warning(
                "US-073 registry: connection %s NOT FOUND in config — available %d, "
                "rejected without connection %d (fail-closed)",
                connection_id, registered, int(skipped.get("no-connection", 0)),
            )
        # US-037 ВАЖНО: НЕ перезаписываем cc.discovered_models здесь. Это кэш
        # ПОЛНОГО списка моделей подключения (его ведёт диалог подключения через
        # discover_all_models). Старый стартовый STT-discover вызывает
        # set_cloud_models с отфильтрованным STT-подмножеством — если бы мы
        # писали его в discovered_models, полный список затирался бы 2 моделями
        # после каждого перезапуска. set_cloud_models только обновляет живой
        # реестр _CLOUD_MODELS_REGISTRY.

    @staticmethod
    def _log_cloud_registration(conn, registered: int, skipped: dict) -> None:
        """US-073 (AC 9): в журнале видно, сколько моделей скрыто по каждому
        подключению и по какой причине."""
        external = int(skipped.get("external", 0))
        unknown = int(skipped.get("unknown", 0))
        non_stt = int(skipped.get("non-stt", 0))
        no_conn = int(skipped.get("no-connection", 0))
        log.info(
            "US-073 registry: connection %r (%s) — available %d, hidden by Cloud.ru filter %d "
            "(external %d, placement unknown %d), skipped as non-STT %d, "
            "rejected without connection %d",
            getattr(conn, "name", "") or getattr(conn, "id", "?"),
            getattr(conn, "id", "?"), registered, external + unknown, external, unknown,
            non_stt, no_conn,
        )

    def _register_cloud_model(self, connection_id: str, conn_type: str, model_id: str) -> str:
        """US-037: добавить одну cloud-модель в _CLOUD_MODELS_REGISTRY.
        Ключ формата cloud:<connection_id>:<model_id>.

        US-073 ЧОКПОИНТ: это ЕДИНСТВЕННАЯ точка регистрации облачной модели,
        поэтому здесь же стоят оба отбора — фильтр размещения (внешние модели
        Cloud.ru) и отбор STT-моделей. Любой путь регистрации (восстановление
        из настроек, ответ проверки соединения, стартовая проверка) проходит
        через этот метод, значит внешняя модель не может попасть в списки в
        обход фильтра. Не переносить эти проверки в вызывающий код.

        Возвращает "" — модель зарегистрирована; иначе причину отказа:
        "external"/"unknown" — фильтр размещения, "non-stt" — модель не
        распознаёт речь, "bad" — пустые аргументы или неизвестный тип,
        "no-connection" — подключение не найдено (fail-closed, см. ниже).
        """
        if not model_id or not connection_id:
            return "bad"
        if conn_type == "openai":
            engine = CLOUD_OPENAI_ENGINE
            display = "OpenAI"
        elif conn_type == "elevenlabs":
            engine = CLOUD_ELEVENLABS_ENGINE
            display = "ElevenLabs"
        else:
            return "bad"
        conn = None
        if self._last_cfg is not None:
            try:
                conn = self._last_cfg.connection_by_id(connection_id)
            except Exception:  # noqa: BLE001
                conn = None
        if conn is None:
            # US-073 FAIL-CLOSED. Без подключения неизвестны ни размещение
            # модели, ни состояние флажка «только модели Cloud.ru», поэтому
            # регистрировать нельзя: молча открыть внешнюю модель хуже, чем
            # не показать её вовсе (требование ИТ-директора, 152-ФЗ).
            # Случай реален: если refresh_cloud_models упал на битой записи
            # в config.json, приложение продолжает работу с _last_cfg=None, и
            # стартовая проверка соединения через 1.5 с зарегистрировала бы
            # модели вообще без фильтра. Модели несуществующего подключения
            # всё равно неработоспособны — resolve_cloud_connection не найдёт
            # для них ни ключа, ни адреса.
            log.warning(
                "US-073 registry: connection %s not resolved (cfg loaded: %s) — model %r NOT registered "
                "(fail-closed: placement and filter state unknown)",
                connection_id, self._last_cfg is not None, model_id,
            )
            return "no-connection"
        # US-073: внешние модели (metadata.provider = external) в списки не попадают.
        reason = cloud_placement.connection_hidden_reason(conn, model_id)
        if reason:
            return reason
        # TASK-365: STT определяется явным типом сервиса (metadata.type =
        # audio-to-text), а при его отсутствии — прежним разбором имени.
        # ElevenLabs — всегда STT (у этого эндпоинта других моделей нет).
        if conn_type == "openai":
            try:
                from . import cloud_stt as _cs
                declared = cloud_placement.connection_model_type(conn, model_id)
                if not _cs.is_stt_model(model_id, declared):
                    return "non-stt"
            except Exception:  # noqa: BLE001
                pass
        key = f"cloud:{connection_id}:{model_id}"
        spec_obj = _make_cloud_spec(engine, display, model_id)
        _CLOUD_MODELS_REGISTRY[key] = ModelSpec(
            key=key,
            engine=spec_obj.engine,
            name=spec_obj.name,
            repo_id="",
            loader_name=model_id,
            language_hint=spec_obj.language_hint,
            size_hint=spec_obj.size_hint,
            note=spec_obj.note,
        )
        return ""

    def is_installed(self, key: str) -> bool:
        """Return True only for fully downloaded, loadable app-managed models.

        Для cloud-моделей возвращает True, если задан соответствующий API-ключ
        (нет смысла говорить о «загрузке» для облака).
        """
        if is_cloud_model_key(key):
            return self.is_available(key)
        if key not in ALL_MODELS:
            return False
        spec = self.spec(key)
        return _is_complete_model_dir(spec, self.model_path(key))

    def is_incomplete(self, key: str) -> bool:
        if is_cloud_model_key(key):
            return False
        if key not in ALL_MODELS:
            return False
        path = self.model_path(key)
        if not path.exists():
            return False
        if _is_summary_model(self.spec(key)):
            # Summary model dir exists but GGUF file missing or too small
            return not self.is_installed(key)
        return any(path.iterdir()) and not self.is_installed(key)

    def is_available(self, key: str) -> bool:
        """Return whether the model can be selected by the user.

        Whisper Small is available by default through faster-whisper's named model
        loader/cache even when there is no app-managed local directory yet.
        All other transcription models must be explicitly downloaded through the Models tab.
        Summary models are available once downloaded.
        Cloud models — доступны если задан соответствующий API-ключ.
        """
        if is_cloud_model_key(key):
            if key not in _CLOUD_MODELS_REGISTRY:
                return False
            if self._last_cfg is None:
                return False
            # US-037: доступна, если подключение существует и имеет ключ.
            conn = resolve_cloud_connection(self._last_cfg, key)
            return bool(conn and conn.api_key)
        if key == DEFAULT_MODEL_KEY:
            return True
        if key in SUMMARY_MODELS:
            return self.is_installed(key)
        if key not in TRANSCRIPTION_MODELS:
            return False
        return self.is_installed(key)

    def installed_status(self, key: str) -> str:
        if is_cloud_model_key(key):
            return "Облачная" if self.is_available(key) else "Не настроена"
        if self.is_installed(key):
            return "Загружена"
        if self.is_incomplete(key):
            return "Загрузка не завершена"
        if key == DEFAULT_MODEL_KEY:
            # US-051: не утверждаем «Готова по умолчанию» безусловно. Small грузится из
            # кэша faster-whisper; если валидного model.bin в кэше нет (свежая машина или
            # оборванная закачка) — статус «готова» вводит в заблуждение, а диктовка падает
            # 'Unable to open file model.bin'. Показываем реальное состояние. Доступность
            # (is_available) НЕ трогаем: модель остаётся выбираемой, чтобы сработали
            # первая авто-загрузка и self-heal US-052.
            if _whisper_model_cached(self.spec(key).repo_id):
                return "Готова по умолчанию"
            return "Не загружена"
        return "Не загружена"

    def is_transcription_model(self, key: str) -> bool:
        if key in TRANSCRIPTION_MODELS:
            return True
        return is_cloud_model_key(key) and key in _CLOUD_MODELS_REGISTRY

    def is_additional_model(self, key: str) -> bool:
        return key in ADDITIONAL_MODELS

    def is_summary_model(self, key: str) -> bool:
        return key in SUMMARY_MODELS

    def summary_model_gguf_path(self, key: str) -> Path:
        """Return the full path to the GGUF file for a summary model."""
        spec = self.spec(key)
        return self.model_path(key) / spec.loader_name

    def available_summary_model_keys(self) -> list[str]:
        """Return keys of summary models that are downloaded and ready."""
        return [key for key in SUMMARY_MODELS if self.is_installed(key)]

    def available_model_keys(self) -> list[str]:
        keys = [key for key in TRANSCRIPTION_MODELS if self.is_available(key)]
        if DEFAULT_MODEL_KEY not in keys:
            keys.insert(0, DEFAULT_MODEL_KEY)
        # Добавляем cloud-модели, для которых задан API-ключ (US-015, US-016)
        for ckey in _CLOUD_MODELS_REGISTRY:
            if self.is_available(ckey):
                keys.append(ckey)
        return keys

    def download(self, key: str, progress: Optional[Progress] = None, *, hf_token: str | None = None) -> Path:
        if is_cloud_model_key(key):
            raise ValueError("Cloud-модели не нужно скачивать — они используются через API")
        spec = self.spec(key)
        final_path = self.model_path(key)
        staging_path = final_path.with_name(final_path.name + ".downloading")
        log.info("Start model download: %s -> %s", key, final_path)
        with self._lock_for(key):
            self._loaded.pop(key, None)
            _emit_download_progress(progress, f"Подготавливаю загрузку {spec.engine} — {spec.name}…", percent=0, phase="prepare")
            _safe_rmtree(staging_path, retries=2)
            if final_path.exists() and not _is_complete_model_dir(spec, final_path):
                log.warning("Remove incomplete model directory before redownload: %s", final_path)
                _safe_rmtree(final_path, retries=2)
            staging_path.mkdir(parents=True, exist_ok=True)

            token = _resolve_hf_token(hf_token)
            if _is_summary_model(spec):
                total_bytes = _single_file_size_bytes(spec.repo_id, spec.loader_name, token=token) or 0
            else:
                total_bytes = _repo_size_bytes(spec.repo_id, token=token) or 0
            stop_monitor = threading.Event()

            def monitor_download() -> None:
                last_percent: int | None = None
                last_emit_at = 0.0
                while not stop_monitor.is_set():
                    downloaded = _directory_size(staging_path)
                    percent = None
                    if total_bytes > 0:
                        percent = max(0, min(99, int(downloaded * 100 / total_bytes)))
                    now = time.monotonic()
                    if percent != last_percent or now - last_emit_at >= 1.5:
                        size_text = _format_bytes(downloaded)
                        total_text = _format_bytes(total_bytes) if total_bytes > 0 else "…"
                        _emit_download_progress(
                            progress,
                            f"Загружаю {spec.engine} — {spec.name}: {size_text} / {total_text}",
                            percent=percent,
                            downloaded_bytes=downloaded,
                            total_bytes=total_bytes,
                            phase="download",
                        )
                        last_percent = percent
                        last_emit_at = now
                    stop_monitor.wait(0.5)

            monitor_thread = threading.Thread(target=monitor_download, name=f"download-progress-{key}", daemon=True)
            monitor_thread.start()
            try:
                if _is_summary_model(spec):
                    # Summary GGUF: download single file directly into staging_path.
                    # hf_hub_download with local_dir places the file under staging_path
                    # so our directory-size monitor can track real progress.
                    hf_hub_download(
                        repo_id=spec.repo_id,
                        filename=spec.loader_name,
                        local_dir=str(staging_path),
                        token=token,
                    )
                else:
                    snapshot_download(
                        repo_id=spec.repo_id,
                        local_dir=str(staging_path),
                        local_dir_use_symlinks=False,
                        resume_download=False,
                        token=token,
                    )
            finally:
                stop_monitor.set()
                monitor_thread.join(timeout=2.0)

            downloaded = _directory_size(staging_path)
            _emit_download_progress(
                progress,
                "Проверяю целостность файлов модели…",
                percent=100 if total_bytes > 0 else None,
                downloaded_bytes=downloaded,
                total_bytes=total_bytes,
                phase="verify",
            )
            missing = _missing_required_files(spec, staging_path)
            if missing:
                raise RuntimeError(
                    "Загрузка не завершилась корректно: не найдены обязательные файлы: "
                    + ", ".join(missing)
                    + ". Проверьте интернет-соединение и запустите загрузку ещё раз."
                )
            if final_path.exists():
                _safe_rmtree(final_path, retries=3)
            final_path.parent.mkdir(parents=True, exist_ok=True)
            staging_path.replace(final_path)
            _emit_download_progress(progress, "Загрузка завершена.", percent=100, downloaded_bytes=downloaded, total_bytes=total_bytes, phase="done")
            log.info("Model download finished: %s", key)
            return final_path

    def delete(self, key: str) -> None:
        if is_cloud_model_key(key):
            raise ValueError("Cloud-модели нельзя удалить — отключите API-ключ в настройках")
        path = self.model_path(key)
        staging_path = path.with_name(path.name + ".downloading")
        log.info("Delete model: %s from %s", key, path)
        with self._lock_for(key):
            self._loaded.pop(key, None)
            gc.collect()
            _safe_rmtree(path, retries=3)
            _safe_rmtree(staging_path, retries=2)

    def preload(self, key: str, cfg: AppConfig) -> None:
        if is_cloud_model_key(key):
            # Cloud-модели не требуют preload — API всегда «горячий»
            log.info("Skip preload for cloud model: %s", key)
            return
        spec = self.spec(key)
        if not self.is_available(key) or not self.is_transcription_model(key):
            log.info("Skip preload for unavailable/non-transcription model: %s", key)
            return
        with self._lock_for(key):
            if spec.engine == "Whisper":
                self._load_whisper(spec, cfg)
            elif spec.engine == "Parakeet":
                self._load_parakeet(spec)

    def transcribe(self, key: str, wav_path: Path, cfg: AppConfig, *, is_live: bool = False, progress_callback=None, duration_seconds: float = 0.0) -> str:
        spec = self.spec(key)
        if not self.is_available(key):
            raise RuntimeError(f"Модель {model_display_name(key)} не загружена. Сначала загрузите её во вкладке «Модели».")
        # Cloud-модели (US-015, US-016, US-032) — отдельная ветка, без блокировки
        # (несколько cloud-запросов могут идти параллельно — это нормально).
        # US-022: прогресс процента применяется ТОЛЬКО к локальным движкам
        # (Whisper/Parakeet); cloud-путь его игнорирует.
        if is_cloud_model_key(key):
            return self._transcribe_cloud(key, wav_path, cfg)
        with self._lock_for(key):
            transcription_log.info("Transcription start: key=%s engine=%s live=%s language=%s path=%s", key, spec.engine, is_live, _normalize_language(cfg.language), wav_path)
            if spec.engine == "Whisper":
                text = self._transcribe_whisper(spec, wav_path, cfg, is_live=is_live, progress_callback=progress_callback, duration_seconds=duration_seconds)
            elif spec.engine == "Parakeet":
                text = self._transcribe_parakeet(spec, wav_path, is_live=is_live, progress_callback=progress_callback)
            else:
                raise RuntimeError(f"Неподдерживаемый движок: {spec.engine}")
            transcription_log.info("Transcription done: key=%s live=%s chars=%s", key, is_live, len(text))
            return text

    def _transcribe_cloud(
        self,
        key: str,
        wav_path: Path,
        cfg: AppConfig,
        *,
        on_chunk_done=None,
        cancel_check=None,
        chunk_local_fallback=None,
        openai_prompt: "str | None" = None,
        with_timestamps: bool = False,
        with_diarization: bool = False,
        speaker_count: str = "auto",
        on_segments_final=None,
        precut_chunks=None,
    ) -> str:
        """Расшифровка через облачный STT (US-015, US-016) с автонарезкой (US-032).

        Проактивно проверяем интернет (US-015 решение F), затем шлём один
        запрос или несколько чанков. Исключения cloud_stt.* пробрасываются
        наверх — их перехватывает transcribe_with_fallback() для переключения
        на локальную модель.

        TASK-078/TASK-079 (US-017): on_chunk_done и cancel_check пробрасываются
        в split_and_transcribe для прогрессивных block_ready и отзывчивой отмены.

        TASK-057..060 (US-017): при with_timestamps cloud-функции возвращают
        (text, segments). Для OpenAI диаризация выполняется пост-процессом
        локально (assign_speakers), для ElevenLabs — нативно (diarize=true).
        Итоговые сегменты прогоняются через merge-функции (как локальный путь),
        чтобы текст был фразового уровня, и отдаются через on_segments_final.
        Возвращается всегда str (полный текст) — сегменты идут через колбэк.
        """
        from . import cloud_stt

        # US-037: реквизиты берутся из подключения, на которое ссылается ключ
        # (cloud:<connection_id>:<model_id>), а не из устаревших cfg.*_stt_*.
        conn = resolve_cloud_connection(cfg, key)
        if conn is None:
            raise RuntimeError(
                "Облачное подключение для выбранной модели не найдено "
                "(возможно, удалено). Выберите подключение на вкладке «Модели»."
            )
        provider = conn.type
        model_id = cloud_model_id_of(key)
        language = _normalize_language(cfg.language)
        max_chunk = max(10, int(getattr(cfg, "cloud_max_chunk_seconds", 60) or 60))
        transcription_log.info(
            "Cloud STT start: key=%s provider=%s model=%s language=%s path=%s max_chunk=%ds timestamps=%s diarization=%s",
            key, provider, model_id, language or "auto", wav_path, max_chunk, with_timestamps, with_diarization,
        )

        if provider == "openai":
            host = cloud_stt._host_from_url(conn.base_url)

            def _one(chunk_path: Path):
                return cloud_stt.transcribe_openai_compatible(
                    chunk_path,
                    api_key=conn.api_key,
                    base_url=conn.base_url,
                    model_id=model_id or "whisper-1",
                    language=language,
                    prompt=openai_prompt,  # US-035: только для диктовки
                    with_timestamps=with_timestamps,
                )

            result = cloud_stt.split_and_transcribe(
                wav_path,
                _one,
                max_chunk_seconds=max_chunk,
                require_internet_host=host,
                on_chunk_done=on_chunk_done,
                cancel_check=cancel_check,
                chunk_local_fallback=chunk_local_fallback,
                with_timestamps=with_timestamps,
                precut_chunks=precut_chunks,  # US-040
            )
        elif provider == "elevenlabs":
            def _one(chunk_path: Path):
                return cloud_stt.transcribe_elevenlabs(
                    chunk_path,
                    api_key=conn.api_key,
                    model_id=model_id or "scribe_v1",
                    language=language,
                    with_timestamps=with_timestamps,
                    with_diarization=with_diarization,  # ElevenLabs: нативная диаризация
                )

            result = cloud_stt.split_and_transcribe(
                wav_path,
                _one,
                max_chunk_seconds=max_chunk,
                require_internet_host=cloud_stt.ELEVENLABS_HOST,
                on_chunk_done=on_chunk_done,
                cancel_check=cancel_check,
                chunk_local_fallback=chunk_local_fallback,
                with_timestamps=with_timestamps,
                precut_chunks=precut_chunks,  # US-040
            )
        else:
            raise RuntimeError(f"Неподдерживаемый cloud-провайдер: {provider}")

        if with_timestamps and isinstance(result, tuple):
            text, segments = result
            segments = self._postprocess_cloud_segments(
                wav_path, segments, provider=provider,
                with_diarization=with_diarization, speaker_count=speaker_count,
            )
            if on_segments_final is not None and segments:
                try:
                    on_segments_final(segments)
                except Exception:  # noqa: BLE001
                    log.exception("on_segments_final callback failed")
        else:
            text = result if isinstance(result, str) else (result[0] if result else "")
        transcription_log.info("Cloud STT done: key=%s chars=%s", key, len(text))
        return text

    def _postprocess_cloud_segments(
        self,
        wav_path: Path,
        segments: list,
        *,
        provider: str,
        with_diarization: bool,
        speaker_count: str = "auto",
    ) -> list:
        """TASK-060 (US-017): пост-обработка cloud-сегментов с таймкодами.

        1) Для OpenAI при with_diarization — локальная диаризация поверх
           cloud-результата с таймкодами (assign_speakers). ElevenLabs метки
           спикеров уже проставил нативно.
        2) Склейка коротких сегментов в фразы (merge_whisper_blocks_into_utterances),
           как на локальном пути — чтобы не было «по 2-3 слова на строку».
        3) При диаризации — дополнительная склейка соседних реплик одного спикера.

        Возвращает список dict {start, end, text, speaker}.
        """
        if not segments:
            return segments
        segs: list[TranscriptSegment] = [
            TranscriptSegment(
                float(s.get("start", 0.0)), float(s.get("end", 0.0)),
                (s.get("text") or "").strip(), (s.get("speaker") or ""),
            )
            for s in segments
            if (s.get("text") or "").strip()
        ]
        if not segs:
            return []
        # OpenAI: диаризация — локальный пост-процесс поверх cloud-таймкодов.
        if with_diarization and provider == "openai":
            try:
                from .diarization import assign_speakers

                segs = assign_speakers(wav_path, segs, speaker_count=str(speaker_count or "auto"))
                transcription_log.info("Cloud OpenAI post-process diarization: %d segments", len(segs))
            except Exception as exc:  # noqa: BLE001
                log.exception("Cloud post-process diarization failed: %s", exc)
        # Склейка в фразы (как локальный путь).
        merged = merge_whisper_blocks_into_utterances(segs)
        if with_diarization and len(merged) > 1:
            merged = merge_speaker_segments(merged)
        return [
            {"start": m.start_seconds, "end": m.end_seconds, "text": m.text, "speaker": m.speaker or ""}
            for m in merged
        ]

    def transcribe_with_fallback(
        self,
        key: str,
        wav_path: Path,
        cfg: AppConfig,
        *,
        is_live: bool = False,
        on_cloud_chunk=None,
        cancel_check=None,
        chunk_local_fallback=None,
        openai_prompt: "str | None" = None,
        with_timestamps: bool = False,
        with_diarization: bool = False,
        speaker_count: str = "auto",
        on_segments_final=None,
        progress_callback=None,
        duration_seconds: float = 0.0,
        precut_chunks=None,
    ) -> tuple[str, bool, str, str]:
        """Обёртка над transcribe() с автоматическим fallback на локальную модель.

        Возвращает кортеж (text, used_fallback, fallback_key, reason).
        Если used_fallback=True, cfg.selected_model уже переключён на
        cfg.cloud_fallback_model_key и сохранён в config.json
        (US-015/US-016 решение G).

        TASK-057..060 (US-017): with_timestamps/with_diarization/speaker_count и
        on_segments_final пробрасываются в _transcribe_cloud для cloud-расшифровки
        файлов с таймкодами и диаризацией. Итоговые сегменты доставляются через
        on_segments_final (текст по-прежнему возвращается строкой в кортеже).
        """
        # Не cloud → как обычно (колбэки игнорируются — локальные модели
        # обрабатываются через transcribe_file_progressive со своим прогрессом)
        if not is_cloud_model_key(key):
            # US-022: прогресс процента диктовки (overlay/статус) для локальных моделей.
            text = self.transcribe(
                key, wav_path, cfg, is_live=is_live,
                progress_callback=progress_callback, duration_seconds=duration_seconds,
            )
            return text, False, key, ""

        # Cloud → пробуем напрямую через _transcribe_cloud с колбэками,
        # при сбое — fallback на локальную модель.
        from . import cloud_stt as _cs

        try:
            text = self._transcribe_cloud(
                key, wav_path, cfg,
                on_chunk_done=on_cloud_chunk,
                cancel_check=cancel_check,
                chunk_local_fallback=chunk_local_fallback,
                openai_prompt=openai_prompt,  # US-035
                with_timestamps=with_timestamps,
                with_diarization=with_diarization,
                speaker_count=speaker_count,
                on_segments_final=on_segments_final,
                precut_chunks=precut_chunks,  # US-040
            )
            return text, False, key, ""
        except _cs.CloudSttError as exc:
            fallback_key = cfg.cloud_fallback_model_key or DEFAULT_MODEL_KEY
            reason = self._format_cloud_error_reason(exc)
            log.warning("Cloud STT failed (%s), falling back to %s: %s", key, fallback_key, exc)
            # Переключаем selected_model и сохраняем (решение G)
            try:
                cfg.selected_model = fallback_key
                cfg.save()
            except Exception as save_exc:  # noqa: BLE001
                log.warning("Failed to save cfg after fallback: %s", save_exc)
            # Делаем fallback-расшифровку локальной моделью (US-022: с прогрессом)
            text = self.transcribe(
                fallback_key, wav_path, cfg, is_live=is_live,
                progress_callback=progress_callback, duration_seconds=duration_seconds,
            )
            return text, True, fallback_key, reason

    @staticmethod
    def _format_cloud_error_reason(exc: Exception) -> str:
        # Поздний импорт чтобы не создавать жёсткую связь
        from . import cloud_stt as _cs
        if isinstance(exc, _cs.CloudNetworkError):
            return "нет соединения с интернетом"
        if isinstance(exc, _cs.CloudAuthError):
            return "неверный API-ключ"
        if isinstance(exc, _cs.CloudPayloadTooLarge):
            return "файл превышает лимит провайдера"
        if isinstance(exc, _cs.CloudRateLimit):
            return "превышен лимит запросов"
        if isinstance(exc, _cs.CloudServerError):
            return "ошибка на стороне провайдера"
        return f"ошибка облачного API: {exc}"

    def transcribe_file_progressive(
        self,
        key: str,
        wav_path: Path,
        cfg: AppConfig,
        *,
        duration_seconds: float,
        progress_callback: Callable[[float, str], None] | None = None,
        block_callback: Callable[..., None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> str:
        """Transcribe a prepared file while emitting progress and visible blocks."""
        spec = self.spec(key)
        if not self.is_available(key):
            raise RuntimeError(f"Модель {model_display_name(key)} не загружена. Сначала загрузите её во вкладке «Модели».")

        def is_cancelled() -> bool:
            return bool(cancel_check and cancel_check())

        def emit_progress(done: float, message: str) -> None:
            if progress_callback is not None:
                progress_callback(max(0.0, min(done, duration_seconds or done)), message)

        with self._lock_for(key):
            transcription_log.info("File progressive transcription start: key=%s engine=%s language=%s path=%s duration=%.3f", key, spec.engine, _normalize_language(cfg.language), wav_path, duration_seconds)
            transcription_log.info("File options: timestamps_visible=%s diarization=%s", bool(getattr(cfg, "file_stable_timestamps_enabled", False)), bool(getattr(cfg, "file_diarization_enabled", False)))
            if spec.engine == "Whisper":
                text = self._transcribe_whisper_progressive(
                    spec, wav_path, cfg, duration_seconds=duration_seconds, progress_callback=emit_progress, block_callback=block_callback, cancel_check=is_cancelled
                )
            elif spec.engine == "Parakeet":
                text = self._transcribe_parakeet_progressive(
                    spec, wav_path, cfg, duration_seconds=duration_seconds, progress_callback=emit_progress, block_callback=block_callback, cancel_check=is_cancelled
                )
            else:
                raise RuntimeError(f"Неподдерживаемый движок: {spec.engine}")
            transcription_log.info("File progressive transcription done: key=%s chars=%s", key, len(text))
            return text

    def _load_whisper(self, spec: ModelSpec, cfg: AppConfig):  # noqa: ANN001
        from faster_whisper import WhisperModel

        model_obj = self._loaded.get(spec.key)
        if model_obj is not None:
            return model_obj
        path = self.model_path(spec.key)
        if self.is_installed(spec.key):
            model_source = str(path)
        else:
            if path.exists():
                log.warning("Local Whisper model directory is incomplete and will be ignored: %s", path)
            model_source = spec.loader_name
        device = cfg.device if cfg.device != "auto" else "auto"
        # US-053: авторизуем АВТО-загрузку модели тем же HF-токеном, что и кнопка «Скачать».
        # faster-whisper качает через huggingface_hub, который читает токен из окружения
        # (HF_TOKEN/HUGGINGFACE_HUB_TOKEN). Без этого докачка при диктовке/расшифровке шла
        # анонимно (warning про rate limits, ниже скорость). Ставим до WhisperModel(...),
        # чтобы авторизация действовала и на первичную загрузку, и на self-heal-перекачку.
        _hf_tok = _resolve_hf_token(getattr(cfg, "hf_token", "") or None)
        if _hf_tok:
            os.environ["HF_TOKEN"] = _hf_tok
            os.environ["HUGGINGFACE_HUB_TOKEN"] = _hf_tok
        log.info("Load Whisper model: key=%s source=%s device=%s compute=%s token=%s", spec.key, model_source, device, cfg.compute_type, "yes" if _hf_tok else "no")
        try:
            model_obj = WhisperModel(model_source, device=device, compute_type=cfg.compute_type)
        except Exception as exc:  # noqa: BLE001
            # US-052: авто-загрузка из кэша HuggingFace может наткнуться на битый/недокачанный
            # model.bin (обрыв прошлой закачки). faster-whisper сверяет только etag и
            # переиспользует испорченный файл -> 'Unable to open file model.bin' при каждом
            # запуске. Один раз чистим кэш этого репозитория и перекачиваем начисто.
            # ТОЛЬКО для загрузки через кэш (model_source == loader_name), не для
            # укомплектованной папки приложения.
            if model_source != spec.loader_name or not _looks_like_corrupt_model_error(exc):
                raise
            log.warning("Load Whisper failed for %s (%s); attempting cache self-heal for repo %s", spec.key, exc, spec.repo_id)
            purged = _purge_hf_cache_for_repo(spec.repo_id)
            log.info("HF cache purge for %s: removed=%s; re-downloading", spec.repo_id, purged)
            try:
                model_obj = WhisperModel(model_source, device=device, compute_type=cfg.compute_type)
            except Exception as exc2:  # noqa: BLE001
                log.error("Whisper re-download after cache purge failed for %s: %s", spec.key, exc2)
                raise RuntimeError(
                    f"Не удалось загрузить модель Whisper «{spec.name}»: файл модели в кэше был "
                    f"повреждён, а перекачать его начисто не удалось. Проверьте интернет-соединение "
                    f"и повторите. Детали: {exc2}"
                ) from exc2
            log.info("Whisper cache self-heal succeeded for %s", spec.key)
        self._loaded[spec.key] = model_obj
        return model_obj

    def _transcribe_whisper(self, spec: ModelSpec, wav_path: Path, cfg: AppConfig, *, is_live: bool = False, progress_callback=None, duration_seconds: float = 0.0) -> str:
        model = self._load_whisper(spec, cfg)
        language = _normalize_language(cfg.language)
        transcription_log.info("Whisper transcribe args: key=%s language=%s live=%s vad=%s", spec.key, language or "auto", is_live, False if is_live else True)
        segments, _info = model.transcribe(
            str(wav_path),
            language=language,
            vad_filter=False if is_live else True,
            beam_size=1 if is_live else 5,
            condition_on_previous_text=False,
            temperature=0.0,
            word_timestamps=False,
        )
        # US-022: faster-whisper отдаёт сегменты ленивым генератором — реальная
        # работа идёт при итерации. Прогресс = seg.end / total. total берём из
        # переданной длительности диктовки, а если её нет — из _info.duration.
        total = float(duration_seconds) if duration_seconds and duration_seconds > 0 else float(getattr(_info, "duration", 0.0) or 0.0)
        parts: list[str] = []
        last_pct = -1
        for seg in segments:
            parts.append(seg.text)
            if progress_callback is not None and total > 0:
                pct = int(max(0.0, min(100.0, float(seg.end) * 100.0 / total)))
                if pct != last_pct:
                    last_pct = pct
                    try:
                        progress_callback(float(pct))
                    except Exception:  # noqa: BLE001
                        pass
        return "".join(parts).strip()

    def _transcribe_whisper_progressive(
        self,
        spec: ModelSpec,
        wav_path: Path,
        cfg: AppConfig,
        *,
        duration_seconds: float,
        progress_callback: Callable[[float, str], None] | None,
        block_callback: Callable[..., None] | None,
        cancel_check: Callable[[], bool],
    ) -> str:
        model = self._load_whisper(spec, cfg)
        language = _normalize_language(cfg.language)
        stable = bool(getattr(cfg, "file_stable_timestamps_enabled", False))
        diarize = bool(getattr(cfg, "file_diarization_enabled", False))
        speaker_timeline = None
        speaker_count = str(getattr(cfg, "file_speaker_count", "auto") or "auto")
        if diarize:
            try:
                if progress_callback is not None:
                    progress_callback(0.0, "Готовлю определение говорящих…")
                transcription_log.info("Diarization timeline started: key=%s speaker_count=%s", spec.key, speaker_count)
                from .diarization import build_speaker_timeline, speaker_for_interval

                speaker_timeline = build_speaker_timeline(wav_path, speaker_count=speaker_count)
                transcription_log.info(
                    "Diarization timeline ready: key=%s speakers=%s intervals=%s windows=%s",
                    spec.key,
                    speaker_timeline.speaker_count,
                    len(speaker_timeline.intervals),
                    speaker_timeline.window_count,
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("Diarization timeline failed, continuing without speakers: %s", exc)
                transcription_log.exception("Diarization unavailable: key=%s error=%s", spec.key, exc)
                speaker_timeline = None
                from .diarization import speaker_for_interval  # type: ignore[no-redef]
        else:
            from .diarization import speaker_for_interval

        transcription_log.info("Whisper file transcribe args: key=%s language=%s stable_timestamps=%s diarization=%s", spec.key, language or "auto", stable, diarize)
        segments, _info = model.transcribe(
            str(wav_path),
            language=language,
            vad_filter=True,
            beam_size=5,
            condition_on_previous_text=False,
            temperature=0.0,
            word_timestamps=stable,
        )
        emitted_blocks: list[TranscriptSegment] = []
        raw_segments: list[TranscriptSegment] = []
        pending: TranscriptSegment | None = None
        last_end = 0.0

        def emit_block(block: TranscriptSegment) -> None:
            text = block.text.strip()
            if not text:
                return
            speaker = speaker_for_interval(speaker_timeline, block.start_seconds, block.end_seconds) if diarize else ""
            labelled = TranscriptSegment(block.start_seconds, block.end_seconds, text, speaker)
            emitted_blocks.append(labelled)
            if block_callback is not None:
                block_callback(labelled.start_seconds, labelled.end_seconds, labelled.text, labelled.speaker, False)
                transcription_log.info(
                    "File ASR segment emitted%s: key=%s start=%.3f end=%.3f speaker=%s chars=%s",
                    " with speaker" if labelled.speaker else "",
                    spec.key,
                    labelled.start_seconds,
                    labelled.end_seconds,
                    labelled.speaker or "",
                    len(labelled.text),
                )

        def should_merge(left: TranscriptSegment, right: TranscriptSegment) -> bool:
            gap = max(0.0, right.start_seconds - left.end_seconds)
            combined_text = (left.text.rstrip() + " " + right.text.lstrip()).strip()
            combined_duration = max(right.end_seconds, left.end_seconds) - left.start_seconds
            if gap <= 1.15 and combined_duration <= 28.0 and len(combined_text) <= 420 and not _ends_strongly(left.text):
                return True
            if len(left.text) < 120 and gap <= 0.55 and combined_duration <= 28.0:
                return True
            return False

        for seg in segments:
            if cancel_check():
                raise InterruptedError("Расшифровка файла отменена пользователем.")
            text = str(getattr(seg, "text", "") or "").strip()
            start = float(getattr(seg, "start", last_end) or last_end)
            end = float(getattr(seg, "end", max(last_end, start)) or max(last_end, start))
            last_end = max(last_end, end)
            if not text:
                continue
            raw = TranscriptSegment(start, end, text)
            raw_segments.append(raw)
            if pending is None:
                pending = raw
            elif should_merge(pending, raw):
                pending = TranscriptSegment(pending.start_seconds, max(pending.end_seconds, raw.end_seconds), (pending.text.rstrip() + " " + raw.text.lstrip()).strip())
            else:
                emit_block(pending)
                pending = raw
            if pending is not None and (_ends_strongly(pending.text) or (pending.end_seconds - pending.start_seconds) >= 24.0 or len(pending.text) >= 360):
                emit_block(pending)
                pending = None
            if duration_seconds > 0:
                percent = int(max(0.0, min(100.0, (last_end / duration_seconds) * 100.0)))
                if progress_callback is not None:
                    progress_callback(last_end, f"{percent}% · обработано {format_duration(last_end)} из {format_duration(duration_seconds)}")

        if pending is not None:
            emit_block(pending)
        merged_preview = merge_whisper_blocks_into_utterances(raw_segments)
        transcription_log.info("Whisper merge: raw_segments=%s emitted_blocks=%s merged_preview=%s", len(raw_segments), len(emitted_blocks), len(merged_preview))
        if diarize and len(emitted_blocks) > 1:
            before_count = len(emitted_blocks)
            emitted_blocks = merge_speaker_segments(emitted_blocks)
            transcription_log.info(
                "Speaker segment merge: before=%s after=%s key=%s",
                before_count, len(emitted_blocks), spec.key,
            )
            # Re-emit merged blocks so the UI displays consolidated speaker segments
            if block_callback is not None:
                block_callback(0.0, 0.0, "", "", True)  # replace_existing=True clears old blocks
                for b in emitted_blocks:
                    block_callback(b.start_seconds, b.end_seconds, b.text, b.speaker, False)
        if diarize:
            transcription_log.info("Speaker labels applied progressively: key=%s blocks=%s", spec.key, sum(1 for b in emitted_blocks if b.speaker))
        parts: list[str] = []
        for block in emitted_blocks:
            if cancel_check():
                raise InterruptedError("Расшифровка файла отменена пользователем.")
            if block.text.strip():
                parts.append(block.text.strip())
        if progress_callback is not None:
            progress_callback(duration_seconds, "100% · файл расшифрован")
        return " ".join(parts).strip()

    def _apply_optional_diarization(self, wav_path: Path, blocks: list[TranscriptSegment], cfg: AppConfig) -> list[TranscriptSegment]:
        """Assign segment-level speaker labels.

        v4.0 downloads the Sortformer ONNX model and keeps the processing local. The
        first implementation uses robust segment-level acoustic clustering as a safe
        fallback while the Sortformer runtime path is isolated from the main ASR flow.
        This keeps the feature available without risking transcription regressions.
        """
        if not blocks:
            return blocks
        try:
            from .diarization import assign_speakers

            speaker_count = str(getattr(cfg, "file_speaker_count", "auto") or "auto")
            labelled = assign_speakers(wav_path, blocks, speaker_count=speaker_count)
            return labelled
        except Exception as exc:  # noqa: BLE001
            log.exception("Diarization failed, continuing without speaker labels: %s", exc)
            return blocks

    def _transcribe_parakeet_progressive(
        self,
        spec: ModelSpec,
        wav_path: Path,
        cfg: AppConfig,
        *,
        duration_seconds: float,
        progress_callback: Callable[[float, str], None] | None,
        block_callback: Callable[..., None] | None,
        cancel_check: Callable[[], bool],
    ) -> str:
        model = self._load_parakeet(spec)
        chunk_len = 24.0
        chunks = split_wav_by_duration(wav_path, chunk_seconds=chunk_len, overlap_seconds=0.0)
        if not chunks:
            return ""
        diarize = bool(getattr(cfg, "file_diarization_enabled", False))
        speaker_timeline = None
        speaker_count = str(getattr(cfg, "file_speaker_count", "auto") or "auto")
        if diarize:
            try:
                if progress_callback is not None:
                    progress_callback(0.0, "Готовлю определение говорящих…")
                transcription_log.info("Diarization timeline started: key=%s speaker_count=%s", spec.key, speaker_count)
                from .diarization import build_speaker_timeline, speaker_for_interval

                speaker_timeline = build_speaker_timeline(wav_path, speaker_count=speaker_count)
                transcription_log.info(
                    "Diarization timeline ready: key=%s speakers=%s intervals=%s windows=%s",
                    spec.key,
                    speaker_timeline.speaker_count,
                    len(speaker_timeline.intervals),
                    speaker_timeline.window_count,
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("Diarization timeline failed, continuing without speakers: %s", exc)
                transcription_log.exception("Diarization unavailable: key=%s error=%s", spec.key, exc)
                speaker_timeline = None
                from .diarization import speaker_for_interval  # type: ignore[no-redef]
        else:
            from .diarization import speaker_for_interval

        blocks: list[TranscriptSegment] = []
        try:
            for index, chunk in enumerate(chunks, start=1):
                if cancel_check():
                    raise InterruptedError("Расшифровка файла отменена пользователем.")
                text = self._recognize_parakeet_chunk(model, chunk.path).strip()
                if text:
                    speaker = speaker_for_interval(speaker_timeline, chunk.start_seconds, chunk.end_seconds) if diarize else ""
                    block = TranscriptSegment(chunk.start_seconds, chunk.end_seconds, text, speaker)
                    blocks.append(block)
                    if block_callback is not None:
                        block_callback(block.start_seconds, block.end_seconds, block.text.strip(), block.speaker, False)
                        transcription_log.info(
                            "File ASR segment emitted%s: key=%s start=%.3f end=%.3f speaker=%s chars=%s",
                            " with speaker" if block.speaker else "",
                            spec.key,
                            block.start_seconds,
                            block.end_seconds,
                            block.speaker or "",
                            len(block.text.strip()),
                        )
                done = chunk.end_seconds
                if duration_seconds > 0:
                    percent = int(max(0.0, min(100.0, (done / duration_seconds) * 100.0)))
                    if progress_callback is not None:
                        progress_callback(done, f"{percent}% · фрагмент {index} из {len(chunks)} · {format_duration(done)} из {format_duration(duration_seconds)}")
            if diarize and len(blocks) > 1:
                before_count = len(blocks)
                blocks = merge_speaker_segments(blocks)
                transcription_log.info(
                    "Speaker segment merge: before=%s after=%s key=%s",
                    before_count, len(blocks), spec.key,
                )
                if block_callback is not None:
                    block_callback(0.0, 0.0, "", "", True)  # replace_existing=True
                    for b in blocks:
                        block_callback(b.start_seconds, b.end_seconds, b.text, b.speaker, False)
            if diarize:
                transcription_log.info("Speaker labels applied progressively: key=%s blocks=%s", spec.key, sum(1 for b in blocks if b.speaker))
            parts: list[str] = []
            for block in blocks:
                if cancel_check():
                    raise InterruptedError("Расшифровка файла отменена пользователем.")
                if block.text.strip():
                    parts.append(block.text.strip())
            if progress_callback is not None:
                progress_callback(duration_seconds, "100% · файл расшифрован")
            return merge_transcript_parts(parts).strip()
        finally:
            for chunk in chunks:
                if chunk.path != wav_path:
                    try:
                        chunk.path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    try:
                        if chunk.path.parent.name.startswith("voice-input-file-chunks-"):
                            chunk.path.parent.rmdir()
                    except Exception:
                        pass

    def _load_parakeet(self, spec: ModelSpec):  # noqa: ANN001
        import onnx_asr

        model_obj = self._loaded.get(spec.key)
        if model_obj is not None:
            return model_obj
        if not self.is_installed(spec.key):
            raise RuntimeError(f"Модель {model_display_name(spec.key)} не загружена. Сначала загрузите её во вкладке «Модели».")
        local_dir = str(self.model_path(spec.key))
        log.info("Load Parakeet model: key=%s model=%s local_dir=%s", spec.key, spec.loader_name, local_dir)
        # onnx-asr expects the supported model name as the first argument and
        # the local directory as the second argument. Passing the directory as
        # the model name raises ModelNotSupportedError.
        model_obj = onnx_asr.load_model(spec.loader_name, local_dir)
        self._loaded[spec.key] = model_obj
        return model_obj

    def _recognize_parakeet_chunk(self, model, chunk: Path) -> str:  # noqa: ANN001
        """Run Parakeet recognition with fallbacks for onnx-asr API variants."""
        errors: list[str] = []
        for method_name in ("recognize", "transcribe"):
            method = getattr(model, method_name, None)
            if method is None:
                continue
            try:
                result = method(str(chunk))
                return _extract_text(result)
            except TypeError as exc:
                errors.append(f"{method_name}(str): {exc}")
        try:
            import soundfile as sf

            audio, sr = sf.read(str(chunk), dtype="float32", always_2d=False)
            method = getattr(model, "recognize", None)
            if method is not None:
                for args, kwargs in [((audio,), {"sample_rate": sr}), ((audio, sr), {}), ((audio,), {})]:
                    try:
                        result = method(*args, **kwargs)
                        return _extract_text(result)
                    except TypeError as exc:
                        errors.append(f"recognize(audio args={len(args)} kwargs={bool(kwargs)}): {exc}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"soundfile fallback: {exc}")
        raise RuntimeError("Parakeet не смог распознать WAV. Варианты вызова не подошли: " + " | ".join(errors))

    def _transcribe_parakeet(self, spec: ModelSpec, wav_path: Path, *, is_live: bool = False, progress_callback=None) -> str:
        if is_live:
            # Keep final Parakeet reliable; Whisper remains the preferred live engine.
            return ""
        model = self._load_parakeet(spec)
        chunks = split_wav_if_needed(wav_path, max_seconds=24.0, overlap_seconds=2.0)
        parts: list[str] = []
        # US-022: у Parakeet нет посегментного генератора — прогресс возможен
        # только по границам чанков (по 24 с). Для коротких диктовок (1 чанк)
        # промежуточных обновлений нет: они попадут под порог 2 с и % не покажут.
        total_chunks = len(chunks)
        try:
            for i, chunk in enumerate(chunks):
                parts.append(self._recognize_parakeet_chunk(model, chunk))
                if progress_callback is not None and total_chunks > 0:
                    pct = int(max(0.0, min(100.0, (i + 1) * 100.0 / total_chunks)))
                    try:
                        progress_callback(float(pct))
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            for chunk in chunks:
                if chunk != wav_path:
                    try:
                        chunk.unlink(missing_ok=True)
                    except Exception:
                        pass
                try:
                    if chunk.parent.name.startswith("voice-input-chunks-"):
                        chunk.parent.rmdir()
                except Exception:
                    pass
        return merge_transcript_parts(parts).strip()


def _extract_text(result: object) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result.strip()
    if hasattr(result, "text"):
        return str(getattr(result, "text")).strip()
    if isinstance(result, dict):
        for key in ("text", "transcript", "result"):
            if key in result:
                return str(result[key]).strip()
    if isinstance(result, (list, tuple)):
        values = [_extract_text(x) for x in result]
        return " ".join(x for x in values if x).strip()
    return str(result).strip()


def split_wav_if_needed(wav_path: Path, max_seconds: float = 24.0, overlap_seconds: float = 2.0) -> list[Path]:
    """Split a wav file into overlapping chunks for Parakeet long-form mode."""
    with wave.open(str(wav_path), "rb") as src:
        channels = src.getnchannels()
        sampwidth = src.getsampwidth()
        framerate = src.getframerate()
        frames = src.getnframes()
        duration = frames / float(framerate)
        if duration <= max_seconds:
            return [wav_path]
        bytes_per_frame = channels * sampwidth
        chunk_frames = int(max_seconds * framerate)
        overlap_frames = int(max(0.0, min(overlap_seconds, max_seconds / 2.0)) * framerate)
        step_frames = max(1, chunk_frames - overlap_frames)
        data = src.readframes(frames)

    tmp_dir = Path(tempfile.mkdtemp(prefix="voice-input-chunks-"))
    out: list[Path] = []
    total_frames = frames
    start_frame = 0
    index = 0
    while start_frame < total_frames:
        index += 1
        end_frame = min(total_frames, start_frame + chunk_frames)
        start_byte = start_frame * bytes_per_frame
        end_byte = end_frame * bytes_per_frame
        chunk_data = data[start_byte:end_byte]
        out_path = tmp_dir / f"chunk-{index:03d}.wav"
        with wave.open(str(out_path), "wb") as dst:
            dst.setnchannels(channels)
            dst.setsampwidth(sampwidth)
            dst.setframerate(framerate)
            dst.writeframes(chunk_data)
        out.append(out_path)
        if end_frame >= total_frames:
            break
        start_frame += step_frames
    return out


def _norm_word(word: str) -> str:
    return "".join(ch for ch in word.lower() if ch.isalnum() or ch in "-_")


def merge_transcript_parts(parts: list[str], max_overlap_words: int = 32) -> str:
    merged_words: list[str] = []
    for part in parts:
        words = part.strip().split()
        if not words:
            continue
        if not merged_words:
            merged_words = words
            continue
        max_n = min(max_overlap_words, len(merged_words), len(words))
        overlap = 0
        for n in range(max_n, 0, -1):
            left = [_norm_word(w) for w in merged_words[-n:]]
            right = [_norm_word(w) for w in words[:n]]
            if left == right:
                overlap = n
                break
        merged_words.extend(words[overlap:])
    return " ".join(merged_words)
