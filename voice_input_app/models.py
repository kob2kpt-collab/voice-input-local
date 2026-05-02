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
from huggingface_hub import HfApi, snapshot_download
try:
    from huggingface_hub.utils import disable_progress_bars

    disable_progress_bars()
except Exception:
    pass

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

TRANSCRIPTION_MODELS: dict[str, ModelSpec] = {**WHISPER_MODELS, **PARAKEET_MODELS}
ALL_MODELS: dict[str, ModelSpec] = {**TRANSCRIPTION_MODELS, **ADDITIONAL_MODELS}
WHISPER_REQUIRED_FILES_BY_KEY: dict[str, tuple[str, ...]] = {
    "whisper:tiny": ("model.bin", "config.json", "tokenizer.json", "preprocessor_config.json", "vocabulary.json"),
    "whisper:base": ("model.bin", "config.json", "tokenizer.json", "preprocessor_config.json", "vocabulary.json"),
    "whisper:small": ("model.bin", "config.json", "tokenizer.json", "preprocessor_config.json", "vocabulary.json"),
    "whisper:medium": ("model.bin", "config.json", "tokenizer.json", "preprocessor_config.json", "vocabulary.json"),
    "whisper:large-v3": ("model.bin", "config.json", "tokenizer.json", "preprocessor_config.json", "vocabulary.json"),
    "whisper:large-v3-turbo": ("model.bin", "config.json", "tokenizer.json", "preprocessor_config.json", "vocabulary.json"),
}
WHISPER_FALLBACK_REQUIRED_FILES = ("model.bin", "config.json", "tokenizer.json")
WHISPER_VOCAB_ALTERNATIVES = ("vocabulary.json", "vocabulary.txt")
PARAKEET_MODEL_SUFFIXES = (".onnx", ".json", ".yaml", ".yml")
ADDITIONAL_REQUIRED_FILES_BY_KEY: dict[str, tuple[str, ...]] = {
    "addon:vad": ("ggml-silero-v6.2.0.bin",),
    "addon:sortformer": ("diar_streaming_sortformer_4spk-v2.1.onnx",),
}


def model_display_name(key: str) -> str:
    spec = ALL_MODELS.get(key)
    if spec is None:
        return key
    return f"{spec.engine} — {spec.name}"


def _has_incomplete_downloads(path: Path) -> bool:
    if not path.exists():
        return False
    return any(p.name.endswith(".incomplete") for p in path.rglob("*"))


def _missing_required_files(spec: ModelSpec, path: Path) -> list[str]:
    missing: list[str] = []
    if not path.exists() or not path.is_dir():
        return ["<model directory>"]
    if _has_incomplete_downloads(path):
        missing.append("*.incomplete")
    if spec.engine == "Whisper":
        required = WHISPER_REQUIRED_FILES_BY_KEY.get(spec.key, WHISPER_FALLBACK_REQUIRED_FILES)
        for name in required:
            file_path = path / name
            if not file_path.is_file() or file_path.stat().st_size <= 0:
                missing.append(name)
        # Unknown/local Whisper conversions may use either vocabulary.json or vocabulary.txt.
        if spec.key not in WHISPER_REQUIRED_FILES_BY_KEY:
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

    def _lock_for(self, key: str) -> threading.RLock:
        if key not in self._locks:
            self._locks[key] = threading.RLock()
        return self._locks[key]

    def spec(self, key: str) -> ModelSpec:
        if key not in ALL_MODELS:
            raise KeyError(f"Неизвестная модель: {key}")
        return ALL_MODELS[key]

    def model_path(self, key: str) -> Path:
        spec = self.spec(key)
        safe = key.replace(":", "_")
        return models_dir() / spec.engine.lower() / safe

    def is_installed(self, key: str) -> bool:
        """Return True only for fully downloaded, loadable app-managed models."""
        if key not in ALL_MODELS:
            return False
        spec = self.spec(key)
        return _is_complete_model_dir(spec, self.model_path(key))

    def is_incomplete(self, key: str) -> bool:
        if key not in ALL_MODELS:
            return False
        path = self.model_path(key)
        return path.exists() and any(path.iterdir()) and not self.is_installed(key)

    def is_available(self, key: str) -> bool:
        """Return whether the model can be selected by the user.

        Whisper Small is available by default through faster-whisper's named model
        loader/cache even when there is no app-managed local directory yet.
        All other transcription models must be explicitly downloaded through the Models tab.
        Unknown, legacy, and additional service models are never selectable for dictation.
        """
        if key == DEFAULT_MODEL_KEY:
            return True
        if key not in TRANSCRIPTION_MODELS:
            return False
        return self.is_installed(key)

    def installed_status(self, key: str) -> str:
        if self.is_installed(key):
            return "Загружена"
        if self.is_incomplete(key):
            return "Загрузка не завершена"
        if key == DEFAULT_MODEL_KEY:
            return "Готова по умолчанию"
        return "Не загружена"

    def is_transcription_model(self, key: str) -> bool:
        return key in TRANSCRIPTION_MODELS

    def is_additional_model(self, key: str) -> bool:
        return key in ADDITIONAL_MODELS

    def available_model_keys(self) -> list[str]:
        keys = [key for key in TRANSCRIPTION_MODELS if self.is_available(key)]
        if DEFAULT_MODEL_KEY not in keys:
            keys.insert(0, DEFAULT_MODEL_KEY)
        return keys

    def download(self, key: str, progress: Optional[Progress] = None, *, hf_token: str | None = None) -> Path:
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
        path = self.model_path(key)
        staging_path = path.with_name(path.name + ".downloading")
        log.info("Delete model: %s from %s", key, path)
        with self._lock_for(key):
            self._loaded.pop(key, None)
            gc.collect()
            _safe_rmtree(path, retries=3)
            _safe_rmtree(staging_path, retries=2)

    def preload(self, key: str, cfg: AppConfig) -> None:
        spec = self.spec(key)
        if not self.is_available(key) or not self.is_transcription_model(key):
            log.info("Skip preload for unavailable/non-transcription model: %s", key)
            return
        with self._lock_for(key):
            if spec.engine == "Whisper":
                self._load_whisper(spec, cfg)
            elif spec.engine == "Parakeet":
                self._load_parakeet(spec)

    def transcribe(self, key: str, wav_path: Path, cfg: AppConfig, *, is_live: bool = False) -> str:
        spec = self.spec(key)
        if not self.is_available(key):
            raise RuntimeError(f"Модель {model_display_name(key)} не загружена. Сначала загрузите её во вкладке «Модели».")
        with self._lock_for(key):
            transcription_log.info("Transcription start: key=%s engine=%s live=%s language=%s path=%s", key, spec.engine, is_live, _normalize_language(cfg.language), wav_path)
            if spec.engine == "Whisper":
                text = self._transcribe_whisper(spec, wav_path, cfg, is_live=is_live)
            elif spec.engine == "Parakeet":
                text = self._transcribe_parakeet(spec, wav_path, is_live=is_live)
            else:
                raise RuntimeError(f"Неподдерживаемый движок: {spec.engine}")
            transcription_log.info("Transcription done: key=%s live=%s chars=%s", key, is_live, len(text))
            return text

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
        log.info("Load Whisper model: key=%s source=%s device=%s compute=%s", spec.key, model_source, device, cfg.compute_type)
        model_obj = WhisperModel(model_source, device=device, compute_type=cfg.compute_type)
        self._loaded[spec.key] = model_obj
        return model_obj

    def _transcribe_whisper(self, spec: ModelSpec, wav_path: Path, cfg: AppConfig, *, is_live: bool = False) -> str:
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
        return "".join(seg.text for seg in segments).strip()

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

    def _transcribe_parakeet(self, spec: ModelSpec, wav_path: Path, *, is_live: bool = False) -> str:
        if is_live:
            # Keep final Parakeet reliable; Whisper remains the preferred live engine.
            return ""
        model = self._load_parakeet(spec)
        chunks = split_wav_if_needed(wav_path, max_seconds=24.0, overlap_seconds=2.0)
        parts: list[str] = []
        try:
            for chunk in chunks:
                parts.append(self._recognize_parakeet_chunk(model, chunk))
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
