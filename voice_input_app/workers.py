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
    # US-022: прогресс процента финальной локальной диктовки (0..100).
    # Эмитится только при is_live=False (для cloud-моделей не эмитится — там нет
    # посегментного прогресса).
    progress = Signal(int)

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
            # US-035: при диктовке передаём Initial Prompt из настроек OpenAI.
            # Для не-cloud моделей и для не-OpenAI cloud провайдеров параметр
            # игнорируется в нижних слоях.
            _prompt_val = getattr(self.cfg, "openai_stt_initial_prompt", "") or None
            # US-035 diag: подтверждение что worker действительно видит свежий cfg.
            _p_len = len(_prompt_val) if _prompt_val else 0
            _p_preview = (_prompt_val[:60] if _prompt_val else "").replace("\n", " ")
            log.info(
                "TranscribeWorker: model=%s is_live=%s openai_prompt_chars=%d preview=%r",
                self.model_key, self.is_live, _p_len, _p_preview,
            )
            # US-022: прогресс процента — только для финальной (не live) диктовки.
            _progress_cb = None if self.is_live else (lambda p: self.progress.emit(int(p)))
            text, used_fallback, fallback_key, reason = self.manager.transcribe_with_fallback(
                self.model_key, self.wav_path, self.cfg, is_live=self.is_live,
                openai_prompt=_prompt_val,
                progress_callback=_progress_cb,
                duration_seconds=float(self.duration or 0.0),
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

                # TASK-057..061 (US-017): флаги таймкодов/диаризации для cloud-файла.
                want_diarization = bool(getattr(self.cfg, "file_diarization_enabled", False))
                # Диаризация требует посегментных таймкодов (спикеры мапятся на
                # интервалы), поэтому при включённой диаризации запрашиваем
                # таймкоды неявно — как на локальном пути, где диаризация не
                # зависит от галки «Точные таймкоды».
                want_timestamps = bool(getattr(self.cfg, "file_stable_timestamps_enabled", False)) or want_diarization
                speaker_count = str(getattr(self.cfg, "file_speaker_count", "auto") or "auto")

                def _on_cloud_chunk(index: int, total: int, start_sec: float, end_sec: float,
                                    text: str, segments=None) -> None:
                    if self._cancel_requested:
                        return
                    chunks_done[0] += 1
                    done = chunks_done[0]
                    # Текст готового куска — в UI прогрессивно (TASK-078). При
                    # with_timestamps финальные структурированные сегменты придут
                    # позже через on_segments_final и заменят эти черновые блоки.
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

                def _on_segments_final(segments) -> None:
                    """TASK-059/060: итоговые сегменты с таймкодами (и спикерами для
                    диаризации) — переэмитим как структурированные блоки, заменяя
                    черновые поблочные (replace_existing на первом)."""
                    if self._cancel_requested or not segments:
                        return
                    first = True
                    for s in segments:
                        seg_text = (s.get("text") or "").strip()
                        if not seg_text:
                            continue
                        self.block_ready.emit(FileTranscriptBlock(
                            start_seconds=float(s.get("start", 0.0)),
                            end_seconds=float(s.get("end", 0.0)),
                            text=seg_text,
                            speaker=(s.get("speaker") or ""),
                            replace_existing=first,
                        ))
                        first = False

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
                    # US-035: при расшифровке файла Initial Prompt НЕ применяем
                    # (по согласованию с владельцем продукта — только для диктовки).
                    text, used_fallback, fallback_key, reason = self.manager.transcribe_with_fallback(
                        self.model_key,
                        prepared_path,
                        self.cfg,
                        is_live=False,
                        on_cloud_chunk=_on_cloud_chunk,
                        cancel_check=lambda: self._cancel_requested,
                        chunk_local_fallback=_chunk_local_fallback,
                        openai_prompt=None,
                        with_timestamps=want_timestamps,
                        with_diarization=want_diarization,
                        speaker_count=speaker_count,
                        on_segments_final=_on_segments_final,
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
    """Run summarization in background thread.

    US-036: поддерживает два режима.
      mode="local"  — локальная LLM (summarizer.summarize). Ошибка -> failed.
      mode="cloud"  — облачная LLM (cloud_llm.summarize_text_cloud). Ошибка
                       API/сети -> cloud_failed(reason) БЕЗ авто-fallback:
                       выбор (локально/отмена) делает UI.
    """
    finished_text = Signal(str)
    failed = Signal(str)          # локальный сбой
    cloud_failed = Signal(str)    # US-036: облачный сбой -> UI предлагает выбор

    def __init__(
        self,
        text: str,
        model_path: str = "",
        system_prompt: str = "",
        *,
        mode: str = "local",
        cloud_api_key: str = "",
        cloud_base_url: str = "",
        cloud_model_id: str = "",
        reasoning: bool = False,
        reasoning_effort: str = "low",
    ) -> None:
        super().__init__()
        self.text = text
        self.model_path = model_path
        self.system_prompt = system_prompt
        self.mode = mode or "local"
        self.cloud_api_key = cloud_api_key
        self.cloud_base_url = cloud_base_url
        self.cloud_model_id = cloud_model_id
        self.reasoning = reasoning
        self.reasoning_effort = reasoning_effort

    def run(self) -> None:
        if self.mode == "cloud":
            try:
                from .cloud_llm import summarize_text_cloud
                result = summarize_text_cloud(
                    self.text,
                    api_key=self.cloud_api_key,
                    base_url=self.cloud_base_url or "https://api.openai.com/v1",
                    model_id=self.cloud_model_id or "gpt-4o-mini",
                    system_prompt=self.system_prompt,
                    reasoning=self.reasoning,
                    reasoning_effort=self.reasoning_effort,
                )
                self.finished_text.emit(result)
            except Exception as exc:  # noqa: BLE001
                log.warning("Cloud summarization failed: %s", exc)
                self.cloud_failed.emit(str(exc))
            return
        try:
            from pathlib import Path
            from .summarizer import summarize
            result = summarize(self.text, model_path=Path(self.model_path), system_prompt=self.system_prompt, reasoning=self.reasoning)
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


class ConnectionVerifyWorker(QThread):
    """US-037: проверка облачного ПОДКЛЮЧЕНИЯ + discover ВСЕХ моделей эндпоинта
    (без фильтра по типу функции). Подключение должно находить полный список
    моделей; отбор по назначению (STT/LLM/...) делается в настройках функций.

    Эмитит result(ok: bool, message: str, models: list[str]).
    """

    result = Signal(bool, str, list)

    def __init__(self, ctype: str, api_key: str, base_url: str = "", connection_id: str = "") -> None:
        super().__init__()
        self.ctype = ctype
        self.api_key = api_key
        self.base_url = base_url
        self.connection_id = connection_id

    def run(self) -> None:
        try:
            if self.ctype == "elevenlabs":
                from . import cloud_stt
                ok, message = cloud_stt.verify_elevenlabs_connection(self.api_key)
                # У ElevenLabs нет /v1/models для STT — известные STT-модели.
                models = ["scribe_v1", "scribe_v1_experimental"] if ok else []
                self.result.emit(ok, message, models)
                return
            # OpenAI-совместимый эндпоинт.
            from . import cloud_llm
            ok, message = cloud_llm.verify_connection(self.api_key, self.base_url)
            models: list[str] = []
            if ok:
                cloud_llm.invalidate_discover_cache()
                try:
                    models = cloud_llm.discover_all_models(self.api_key, self.base_url or None)
                except Exception as exc:  # noqa: BLE001
                    log.warning("discover_all_models failed after verify: %s", exc)
                    models = []
            self.result.emit(ok, message, models)
        except Exception as exc:  # noqa: BLE001
            log.exception("ConnectionVerifyWorker failed")
            self.result.emit(False, f"Сбой проверки: {exc}", [])


class CloudConnectionCheckWorker(QThread):
    """Проверяет соединение с облачным STT-провайдером и обновляет список моделей.

    US-015, US-016. Эмитит result(ok: bool, message: str, models: list[str]).
    Используется кнопкой «Проверить соединение» в настройках.
    """

    result = Signal(bool, str, list)

    def __init__(self, provider: str, api_key: str, base_url: str = "", connection_id: str = "") -> None:
        super().__init__()
        self.provider = provider
        self.api_key = api_key
        self.base_url = base_url
        # US-037: id подключения для корреляции результата в UI (необязателен).
        self.connection_id = connection_id

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


class PostProcessWorker(QThread):
    """US-034: постобработка текста расшифровки облачной LLM вне UI-потока.

    Запускается только для диктовки через облачный STT. При успехе эмитит
    finished_text(улучшенный_текст). При любой ошибке эмитит failed(detail) —
    UI показывает сырой текст и уведомление в статусной строке.
    """
    finished_text = Signal(str)
    failed = Signal(str)

    def __init__(self, text: str, cfg: AppConfig) -> None:
        super().__init__()
        self.text = text
        self.cfg = cfg

    def run(self) -> None:
        try:
            from .cloud_llm import post_process_text

            # US-037: реквизиты из подключения postprocess_connection_id;
            # fallback на устаревшие поля для старых конфигов.
            conn = None
            try:
                conn = self.cfg.connection_by_id(getattr(self.cfg, "postprocess_connection_id", "") or "")
            except Exception:  # noqa: BLE001
                conn = None
            _pp_key = (conn.api_key if conn else getattr(self.cfg, "postprocess_api_key", "")) or ""
            _pp_url = (conn.base_url if conn else getattr(self.cfg, "postprocess_base_url", "")) or "https://api.openai.com/v1"
            result = post_process_text(
                self.text,
                api_key=_pp_key,
                base_url=_pp_url,
                model_id=getattr(self.cfg, "postprocess_model_id", "") or "gpt-4o-mini",
                system_prompt=getattr(self.cfg, "postprocess_system_prompt", "") or "",
                reasoning=bool(getattr(self.cfg, "postprocess_reasoning", False)),
                reasoning_effort=getattr(self.cfg, "postprocess_reasoning_effort", "low") or "low",
            )
            self.finished_text.emit(result or self.text)
        except Exception as exc:  # noqa: BLE001
            log.exception("Post-processing failed")
            self.failed.emit(f"{exc}\n\n{traceback.format_exc()}")


class LlmConnectionCheckWorker(QThread):
    """US-034: проверка соединения с LLM-провайдером постобработки и discover
    списка моделей. Эмитит result(ok: bool, message: str, models: list[str])."""

    result = Signal(bool, str, list)

    def __init__(self, api_key: str, base_url: str = "", connection_id: str = "") -> None:
        super().__init__()
        self.api_key = api_key
        self.base_url = base_url
        # US-037: id подключения для корреляции результата в UI (необязателен).
        self.connection_id = connection_id

    def run(self) -> None:
        try:
            from . import cloud_llm
        except Exception as exc:  # noqa: BLE001
            log.exception("cloud_llm import failed")
            self.result.emit(False, f"Не удалось загрузить модуль cloud_llm: {exc}", [])
            return
        try:
            ok, message = cloud_llm.verify_connection(self.api_key, self.base_url)
            models: list[str] = []
            if ok:
                cloud_llm.invalidate_discover_cache()
                try:
                    models = cloud_llm.discover_chat_models(self.api_key, self.base_url or None)
                except Exception as exc:  # noqa: BLE001
                    log.warning("discover_chat_models failed after successful verify: %s", exc)
                    models = []
            self.result.emit(ok, message, models)
        except Exception as exc:  # noqa: BLE001
            log.exception("LlmConnectionCheckWorker failed")
            self.result.emit(False, f"Сбой проверки: {exc}", [])
