from __future__ import annotations

import traceback
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from .audio_files import cleanup_prepared_file, convert_media_to_wav_16k_mono, format_duration, get_media_duration_seconds
from .audio_recorder import auto_detect_input_device
from .config import AppConfig
from .logger import get_logger
from .models import ModelManager, is_cloud_model_key, model_display_name

log = get_logger("workers")


@dataclass(frozen=True)
class FileProgress:
    percent: int
    processed_seconds: float
    total_seconds: float
    message: str


@dataclass(frozen=True)
class FileTranscriptBlock:
    start_seconds: float
    end_seconds: float
    text: str
    speaker: str = ""
    replace_existing: bool = False


@dataclass(frozen=True)
class MicrophoneAutodetectResult:
    device_id: str
    label: str
    sample_rate: int
    rms_level: float


class DownloadWorker(QThread):
    message = Signal(object)
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, manager: ModelManager, model_key: str, cfg: AppConfig) -> None:
        super().__init__()
        self.manager = manager
        self.model_key = model_key
        self.cfg = cfg

    def run(self) -> None:
        try:
            self.manager.download(self.model_key, progress=self.message.emit, hf_token=self.cfg.hf_token)
            self.finished_ok.emit(self.model_key)
        except Exception as exc:  # noqa: BLE001
            log.exception("Download failed: %s", self.model_key)
            self.failed.emit(f"{exc}\n\n{traceback.format_exc()}")


class PreloadWorker(QThread):
    finished_ok = Signal(str)
    failed = Signal(str, str)

    def __init__(self, manager: ModelManager, model_key: str, cfg: AppConfig) -> None:
        super().__init__()
        self.manager = manager
        self.model_key = model_key
        self.cfg = cfg

    def run(self) -> None:
        try:
            self.manager.preload(self.model_key, self.cfg)
            self.finished_ok.emit(self.model_key)
        except Exception as exc:  # noqa: BLE001
            log.exception("Preload failed: %s", self.model_key)
            self.failed.emit(self.model_key, f"{exc}\n\n{traceback.format_exc()}")


class TranscribeWorker(QThread):
    finished_text = Signal(str, float)
    # US-015/US-016: уведомление о применённом fallback (cloud → локальная).
    # Эмитится ДО finished_text, если cloud упал. Передаёт (fallback_key, reason).
    fallback_applied = Signal(str, str)
    failed = Signal(str)

    def __init__(self, manager: ModelManager, model_key: str, wav_path: Path, duration: float, cfg: AppConfig, *, is_live: bool = False) -> None:
        super().__init__()
        self.manager = manager
        self.model_key = model_key
        self.wav_path = wav_path
        self.duration = duration
        self.cfg = cfg
        self.is_live = is_live

    def run(self) -> None:
        try:
            text, used_fallback, fallback_key, reason = self.manager.transcribe_with_fallback(
                self.model_key, self.wav_path, self.cfg, is_live=self.is_live
            )
            if used_fallback:
                self.fallback_applied.emit(fallback_key, reason)
            self.finished_text.emit(text, self.duration)
        except Exception as exc:  # noqa: BLE001
            log.exception("Transcription failed: model=%s live=%s path=%s", self.model_key, self.is_live, self.wav_path)
            self.failed.emit(f"{exc}\n\n{traceback.format_exc()}")


class FileTranscribeWorker(QThread):
    message = Signal(str)
    progress = Signal(object)
    block_ready = Signal(object)
    finished_text = Signal(str, float, str, str)
    failed = Signal(str)
    cancelled = Signal()
    # TASK-055 (US-017): уведомление UI о том, что cloud упал и применён fallback.
    # Параметры: fallback_key, reason. UI обновляет combo и показывает уведомление.
    fallback_applied = Signal(str, str)

    def __init__(self, manager: ModelManager, model_key: str, input_path: Path, cfg: AppConfig) -> None:
        super().__init__()
        self.manager = manager
        self.model_key = model_key
        self.input_path = input_path
        self.cfg = cfg
        self._cancel_requested = False

    def cancel(self) -> None:
        self._cancel_requested = True

    def _emit_progress(self, processed_seconds: float, total_seconds: float, message: str) -> None:
        percent = 0
        if total_seconds > 0:
            percent = int(max(0.0, min(100.0, processed_seconds * 100.0 / total_seconds)))
        self.progress.emit(FileProgress(percent=percent, processed_seconds=processed_seconds, total_seconds=total_seconds, message=message))
        self.message.emit(message)

    def run(self) -> None:
        prepared_path: Path | None = None
        try:
            if self._cancel_requested:
                self.cancelled.emit()
                return
            if not self.input_path.exists():
                raise FileNotFoundError(f"Файл не найден: {self.input_path}")
            self.message.emit("Подготавливаю аудиофайл…")
            self.progress.emit(FileProgress(percent=0, processed_seconds=0.0, total_seconds=0.0, message="Подготовка файла…"))
            prepared_path, duration = convert_media_to_wav_16k_mono(self.input_path)
            if self._cancel_requested:
                self.cancelled.emit()
                return
            if duration <= 0:
                duration = get_media_duration_seconds(self.input_path)
            self._emit_progress(0.0, duration, f"0% · распознавание файла · 00:00 из {format_duration(duration)}")

            def on_progress(done: float, message: str) -> None:
                self._emit_progress(done, duration, message)

            def on_block(start: float, end: float, text: str, speaker: str = "", replace_existing: bool = False) -> None:
                self.block_ready.emit(FileTranscriptBlock(start_seconds=start, end_seconds=end, text=text, speaker=speaker or "", replace_existing=replace_existing))

            # TASK-052 (US-017): ветка cloud для расшифровки файлов.
            # TASK-078: прогрессивный прогресс — block_ready по мере готовности
            # каждого чанка, percent = chunks_done / total_chunks × 100.
            # TASK-079: отзывчивая отмена — cancel_check пробрасывается в
            # split_and_transcribe, при отмене текущий in-flight чанк
            # дорабатывается (Python не умеет прерывать запущенные потоки),
            # новые не запускаются — ожидание ≤120с (READ_TIMEOUT).
            if is_cloud_model_key(self.model_key):
                self._emit_progress(0.0, duration, f"Отправляю файл в облако: {model_display_name(self.model_key)}…")
                if self._cancel_requested:
                    self.cancelled.emit()
                    return

                # Счётчик готовых чанков для расчёта прогресса
                chunks_done = [0]

                def _on_cloud_chunk(index: int, total: int, start_sec: float, end_sec: float, text: str) -> None:
                    if self._cancel_requested:
                        return
                    chunks_done[0] += 1
                    done = chunks_done[0]
                    # Текст готового куска — в UI прогрессивно (TASK-078)
                    if text:
                        self.block_ready.emit(FileTranscriptBlock(
                            start_seconds=start_sec,
                            end_seconds=end_sec,
                            text=text,
                            speaker="",
                            replace_existing=False,
                        ))
                    # Прогресс по доле готовых чанков
                    if total > 0:
                        processed_seconds = duration * done / total
                        percent = int(min(100.0, done * 100.0 / total))
                        self._emit_progress(
                            processed_seconds,
                            duration,
                            f"{percent}% · cloud · чанк {done}/{total}",
                        )

                # TASK-085: per-chunk local fallback. При rate limit или ошибке cloud
                # отдельного чанка — перерасшифровываем этот чанк локально (через
                # cloud_fallback_model_key), не пробрасывая ошибку всему файлу.
                # Это сохраняет частичный прогресс и таймкоды.
                fb_local_key = self.cfg.cloud_fallback_model_key or "whisper:small"

                def _chunk_local_fallback(chunk_wav_path: Path) -> str:
                    """TASK-085: расшифровать ОДИН чанк локально, чтобы спасти прогресс
                    cloud-файла при ошибке отдельного чанка."""
                    log.info("Per-chunk local fallback: %s with %s", chunk_wav_path.name, fb_local_key)
                    return self.manager.transcribe(fb_local_key, chunk_wav_path, self.cfg, is_live=False)

                try:
                    text, used_fallback, fallback_key, reason = self.manager.transcribe_with_fallback(
                        self.model_key,
                        prepared_path,
                        self.cfg,
                        is_live=False,
                        on_cloud_chunk=_on_cloud_chunk,
                        cancel_check=lambda: self._cancel_requested,
                        chunk_local_fallback=_chunk_local_fallback,
                    )
                except InterruptedError:
                    # TASK-079: split_and_transcribe бросает при отмене
                    self.cancelled.emit()
                    return
                if self._cancel_requested:
                    self.cancelled.emit()
                    return
                final_key = fallback_key if used_fallback else self.model_key
                if used_fallback:
                    log.info("File transcribe cloud→fallback: %s → %s (%s)", self.model_key, fallback_key, reason)
                    self.fallback_applied.emit(fallback_key, reason)
                    # Fallback пошёл через локальную модель — текст уже готов целиком,
                    # эмитим один блок (как раньше для cloud без чанк-прогресса)
                    self.block_ready.emit(FileTranscriptBlock(
                        start_seconds=0.0, end_seconds=duration, text=text,
                        speaker="", replace_existing=False,
                    ))
                self._emit_progress(duration, duration, "100% · готово")
                self.finished_text.emit(text, duration, str(self.input_path), final_key)
                return

            text = self.manager.transcribe_file_progressive(
                self.model_key,
                prepared_path,
                self.cfg,
                duration_seconds=duration,
                progress_callback=on_progress,
                block_callback=on_block,
                cancel_check=lambda: self._cancel_requested,
            )
            if self._cancel_requested:
                self.cancelled.emit()
                return
            self.finished_text.emit(text, duration, str(self.input_path), self.model_key)
        except InterruptedError:
            self.cancelled.emit()
        except Exception as exc:  # noqa: BLE001
            log.exception("File transcription failed: model=%s path=%s", self.model_key, self.input_path)
            self.failed.emit(f"{exc}\n\n{traceback.format_exc()}")
        finally:
            if prepared_path is not None:
                cleanup_prepared_file(prepared_path)


class MicrophoneAutodetectWorker(QThread):
    message = Signal(str)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, cfg: AppConfig) -> None:
        super().__init__()
        self.cfg = cfg

    def run(self) -> None:
        try:
            self.message.emit("Проверяю доступные микрофоны…")
            device_id, label, sample_rate, rms_level = auto_detect_input_device(meeting_compatibility=self.cfg.audio_meeting_compatibility)
            self.finished_ok.emit(MicrophoneAutodetectResult(device_id=device_id, label=label, sample_rate=sample_rate, rms_level=rms_level))
        except Exception as exc:  # noqa: BLE001
            log.exception("Microphone autodetect failed")
            self.failed.emit(f"{exc}\n\n{traceback.format_exc()}")


class SummarizeWorker(QThread):
    """Run summarization in background thread."""
    finished_text = Signal(str)
    failed = Signal(str)

    def __init__(self, text: str, model_path: str, system_prompt: str = "") -> None:
        super().__init__()
        self.text = text
        self.model_path = model_path
        self.system_prompt = system_prompt

    def run(self) -> None:
        try:
            from pathlib import Path
            from .summarizer import summarize
            result = summarize(self.text, model_path=Path(self.model_path), system_prompt=self.system_prompt)
            self.finished_text.emit(result)
        except Exception as exc:  # noqa: BLE001
            log.exception("Summarization failed")
            self.failed.emit(f"{exc}\n\n{traceback.format_exc()}")


class UpdateCheckWorker(QThread):
    finished_result = Signal(object)
    failed = Signal(str)

    def __init__(self, repo: str, current_version: str) -> None:
        super().__init__()
        self.repo = repo
        self.current_version = current_version

    def run(self) -> None:
        try:
            from .updater import check_for_update

            info = check_for_update(self.repo, self.current_version)
            self.finished_result.emit(info)
        except Exception as exc:  # noqa: BLE001
            log.exception("Update check failed")
            self.failed.emit(f"{exc}\n\n{traceback.format_exc()}")


class UpdateDownloadWorker(QThread):
    progress = Signal(int, int)
    finished_path = Signal(str)
    failed = Signal(str)

    def __init__(self, info: object) -> None:
        super().__init__()
        self.info = info

    def run(self) -> None:
        try:
            from .updater import download_update

            path = download_update(self.info, progress=lambda done, total: self.progress.emit(int(done), int(total)))
            self.finished_path.emit(str(path))
        except Exception as exc:  # noqa: BLE001
            log.exception("Update download failed")
            self.failed.emit(f"{exc}\n\n{traceback.format_exc()}")


class CloudConnectionCheckWorker(QThread):
    """Проверяет соединение с облачным STT-провайдером и обновляет список моделей.

    US-015, US-016. Эмитит result(ok: bool, message: str, models: list[str]).
    Используется кнопкой «Проверить соединение» в настройках.
    """

    result = Signal(bool, str, list)

    def __init__(self, provider: str, api_key: str, base_url: str = "") -> None:
        super().__init__()
        self.provider = provider
        self.api_key = api_key
        self.base_url = base_url

    def run(self) -> None:
        try:
            from . import cloud_stt
        except Exception as exc:  # noqa: BLE001
            log.exception("cloud_stt import failed")
            self.result.emit(False, f"Не удалось загрузить модуль cloud_stt: {exc}", [])
            return
        try:
            if self.provider == "openai":
                ok, message = cloud_stt.verify_openai_compatible_connection(self.api_key, self.base_url)
            elif self.provider == "elevenlabs":
                ok, message = cloud_stt.verify_elevenlabs_connection(self.api_key)
            else:
                self.result.emit(False, f"Неизвестный провайдер: {self.provider}", [])
                return
            models: list[str] = []
            if ok:
                cloud_stt.invalidate_discover_cache()
                try:
                    models = cloud_stt.discover_models(self.provider, self.api_key, self.base_url or None)
                except Exception as exc:  # noqa: BLE001
                    log.warning("discover_models failed after successful verify: %s", exc)
                    models = []
            self.result.emit(ok, message, models)
        except Exception as exc:  # noqa: BLE001
            log.exception("CloudConnectionCheckWorker failed")
            self.result.emit(False, f"Сбой проверки: {exc}", [])
