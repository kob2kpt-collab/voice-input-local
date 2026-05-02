from __future__ import annotations

import traceback
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from .audio_files import cleanup_prepared_file, convert_media_to_wav_16k_mono, format_duration, get_media_duration_seconds
from .audio_recorder import auto_detect_input_device
from .config import AppConfig
from .logger import get_logger
from .models import ModelManager

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
            text = self.manager.transcribe(self.model_key, self.wav_path, self.cfg, is_live=self.is_live)
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
