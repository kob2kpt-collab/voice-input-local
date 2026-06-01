"""REST API server for Voice Input Local.

API-01: POST /transcribe, GET /capabilities — file transcription via HTTP.
API-02: summary=true parameter for auto-summarization.
API-03: FIFO queue for concurrent requests.
API-04: Async mode with task_id polling via GET /result/{task_id}.

All processing is local. No data is sent to external services.
"""
import secrets
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue
from typing import Optional

from .config import AppConfig
from .logger import get_logger
from .models import ModelManager, ALL_MODELS, TRANSCRIPTION_MODELS, model_display_name

log = get_logger("api")

# ---------------------------------------------------------------------------
# Task store
# ---------------------------------------------------------------------------

@dataclass
class TranscriptionTask:
    task_id: str
    status: str = "queued"  # queued | processing | done | error
    created_at: float = field(default_factory=time.time)
    text: str = ""
    summary: str = ""
    error: str = ""
    segments: list[dict] = field(default_factory=list)
    model_key: str = ""
    timestamps: bool = False
    diarization: bool = False
    request_summary: bool = False


class TaskStore:
    """Thread-safe store for transcription tasks with automatic cleanup."""

    def __init__(self, max_tasks: int = 200, ttl_seconds: float = 3600.0) -> None:
        self._tasks: OrderedDict[str, TranscriptionTask] = OrderedDict()
        self._lock = threading.Lock()
        self._max = max_tasks
        self._ttl = ttl_seconds

    def create(self, **kwargs) -> TranscriptionTask:
        task = TranscriptionTask(task_id=str(uuid.uuid4()), **kwargs)
        with self._lock:
            self._tasks[task.task_id] = task
            self._cleanup()
        return task

    def get(self, task_id: str) -> Optional[TranscriptionTask]:
        with self._lock:
            return self._tasks.get(task_id)

    def update(self, task_id: str, **kwargs) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                for k, v in kwargs.items():
                    setattr(task, k, v)

    def _cleanup(self) -> None:
        now = time.time()
        expired = [tid for tid, t in self._tasks.items() if now - t.created_at > self._ttl]
        for tid in expired:
            del self._tasks[tid]
        while len(self._tasks) > self._max:
            self._tasks.popitem(last=False)


# ---------------------------------------------------------------------------
# FIFO worker
# ---------------------------------------------------------------------------

class TranscriptionQueue:
    """FIFO queue that processes one transcription at a time (API-03)."""

    def __init__(self, manager: ModelManager, store: TaskStore) -> None:
        self._queue: Queue[tuple[TranscriptionTask, Path, AppConfig]] = Queue()
        self._manager = manager
        self._store = store
        self._thread = threading.Thread(target=self._worker, name="api-queue", daemon=True)
        self._thread.start()

    def enqueue(self, task: TranscriptionTask, audio_path: Path, cfg: AppConfig) -> None:
        self._queue.put((task, audio_path, cfg))

    def _worker(self) -> None:
        while True:
            task, audio_path, cfg = self._queue.get()
            try:
                self._store.update(task.task_id, status="processing")
                log.info("API processing task %s model=%s", task.task_id, task.model_key)

                from .audio_files import convert_media_to_wav_16k_mono, cleanup_prepared_file

                wav_path, duration = convert_media_to_wav_16k_mono(audio_path)
                try:
                    segments: list[dict] = []

                    def on_block(start, end, text, speaker="", replace=False):
                        if text.strip():
                            segments.append({
                                "start": round(float(start), 3),
                                "end": round(float(end), 3),
                                "text": text.strip(),
                                "speaker": speaker or "",
                            })

                    text = self._manager.transcribe_file_progressive(
                        task.model_key,
                        wav_path,
                        cfg,
                        duration_seconds=duration,
                        block_callback=on_block if (task.timestamps or task.diarization) else None,
                        cancel_check=lambda: False,
                    )

                    summary = ""
                    if task.request_summary and text.strip():
                        try:
                            from .summarizer import summarize
                            from .models import ModelManager, SUMMARY_MODELS, DEFAULT_SUMMARY_MODEL_KEY
                            mgr = ModelManager()
                            skey = cfg.selected_summary_model or DEFAULT_SUMMARY_MODEL_KEY
                            if skey in SUMMARY_MODELS and mgr.is_installed(skey):
                                gguf = mgr.summary_model_gguf_path(skey)
                                summary = summarize(text, model_path=gguf, system_prompt=cfg.summary_system_prompt)
                            else:
                                log.warning("API summary skipped: model %s not installed", skey)
                        except Exception as exc:
                            log.exception("API summary failed for task %s: %s", task.task_id, exc)

                    self._store.update(
                        task.task_id,
                        status="done",
                        text=text,
                        summary=summary,
                        segments=segments,
                    )
                    log.info("API task done %s chars=%d", task.task_id, len(text))
                finally:
                    cleanup_prepared_file(wav_path)
            except Exception as exc:
                log.exception("API task failed %s", task.task_id)
                self._store.update(task.task_id, status="error", error=str(exc))
            finally:
                try:
                    audio_path.unlink(missing_ok=True)
                except Exception:
                    pass
                self._queue.task_done()


# ---------------------------------------------------------------------------
# FastAPI application factory
# ---------------------------------------------------------------------------

def create_app(manager: ModelManager, cfg: AppConfig) -> "FastAPI":
    """Create and return the FastAPI application."""
    from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException
    from fastapi.responses import JSONResponse
    import tempfile

    app = FastAPI(title="Voice Input Local API", version="1.0")
    store = TaskStore()
    queue = TranscriptionQueue(manager, store)
    api_key = cfg.api_key.strip()

    def _check_auth(authorization: Optional[str]) -> None:
        if not api_key:
            return  # no key configured — open access
        if not authorization:
            raise HTTPException(status_code=401, detail="Missing Authorization header")
        token = authorization.replace("Bearer ", "").strip()
        if not secrets.compare_digest(token, api_key):
            raise HTTPException(status_code=401, detail="Invalid API key")

    @app.get("/capabilities")
    def capabilities(authorization: Optional[str] = Header(default=None)):
        _check_auth(authorization)
        models = []
        for key, spec in TRANSCRIPTION_MODELS.items():
            models.append({
                "key": key,
                "engine": spec.engine,
                "name": spec.name,
                "language": spec.language_hint,
                "available": manager.is_available(key),
            })
        return {
            "models": models,
            "features": ["transcribe", "timestamps", "diarization", "summary"],
            "default_model": cfg.selected_model,
        }

    @app.post("/transcribe")
    async def transcribe(
        file: UploadFile = File(...),
        model: str = Form(default=""),
        timestamps: bool = Form(default=False),
        diarization: bool = Form(default=False),
        summary: bool = Form(default=False),
        authorization: Optional[str] = Header(default=None),
    ):
        """Submit a transcription task. Returns immediately with task_id (API-04)."""
        _check_auth(authorization)

        model_key = model.strip() or cfg.selected_model
        if model_key not in TRANSCRIPTION_MODELS:
            raise HTTPException(status_code=400, detail=f"Unknown model: {model_key}")
        if not manager.is_available(model_key):
            raise HTTPException(status_code=400, detail=f"Model not downloaded: {model_key}")

        # Save uploaded file to temp
        suffix = Path(file.filename or "audio.wav").suffix or ".wav"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, prefix="api-upload-")
        content = await file.read()
        tmp.write(content)
        tmp.close()
        audio_path = Path(tmp.name)

        task_cfg = AppConfig.load()
        task_cfg.file_stable_timestamps_enabled = timestamps
        task_cfg.file_diarization_enabled = diarization

        task = store.create(
            model_key=model_key,
            timestamps=timestamps,
            diarization=diarization,
            request_summary=summary,
        )
        queue.enqueue(task, audio_path, task_cfg)

        return JSONResponse(
            status_code=202,
            content={"task_id": task.task_id, "status": "queued"},
        )

    @app.get("/result/{task_id}")
    def get_result(task_id: str, authorization: Optional[str] = Header(default=None)):
        """Poll for task result (API-04)."""
        _check_auth(authorization)
        task = store.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")

        response: dict = {"task_id": task.task_id, "status": task.status}
        if task.status == "done":
            response["text"] = task.text
            if task.segments:
                response["segments"] = task.segments
            if task.summary:
                response["summary"] = task.summary
        elif task.status == "error":
            response["error"] = task.error
        return response

    return app


def run_api_server(manager: ModelManager, cfg: AppConfig) -> None:
    """Start the API server in a daemon thread."""
    import uvicorn

    app = create_app(manager, cfg)
    port = cfg.api_port or 8672
    host = (getattr(cfg, "api_host", "") or "127.0.0.1").strip() or "127.0.0.1"
    log.info("Starting API server on %s:%d", host, port)

    def _serve():
        uvicorn.run(app, host=host, port=port, log_level="warning")

    thread = threading.Thread(target=_serve, name="api-server", daemon=True)
    thread.start()
    log.info("API server thread started on %s:%d", host, port)
