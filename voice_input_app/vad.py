"""VAD-обёртка для вырезания тишины перед отправкой звука в облачный STT.

EPIC-10 / US-039. Цель — убрать галлюцинации Whisper на участках тишины и в
паузах: облачные модели (в отличие от локального faster-whisper с vad_filter)
получают сырой звук и «додумывают» фразы в паузах. Здесь мы локально находим
речь и отправляем в облако только её.

Движок — Silero VAD, встроенный в faster-whisper (faster_whisper.vad). Он не
требует загруженной модели Whisper и не создаёт ресурсного конфликта с
локальной расшифровкой (разные рантаймы: CTranslate2 vs ONNX Runtime; ORT
InferenceSession потокобезопасен). Ноль новых зависимостей — onnxruntime уже
присутствует транзитивно.

Абстракция намеренно тонкая: если внутренний API faster_whisper.vad окажется
нестабильным между версиями, достаточно переписать detect_speech_segments()
на пакет silero-vad, не трогая вызовы выше (workers.py) и UI.

Применяется ТОЛЬКО к облачной диктовке (single-request, без таймкодов).
Файловый путь (таймкоды/диаризация/нарезка на чанки) здесь не затрагивается.
"""

from __future__ import annotations

import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .audio_files import cleanup_prepared_file, convert_media_to_wav_16k_mono
from .logger import get_logger

log = get_logger("vad")

# Частота, на которой работает Silero VAD и в которой мы отдаём обрезанный звук.
VAD_SAMPLE_RATE = 16000

# Минимальная суммарная длительность речи, ниже которой считаем, что речи нет
# (случай «тишина» — спурьёзные щелчки/шум). Возвращаем no_speech, в облако не
# идём. Порог намеренно мал, чтобы не терять короткие реплики вроде «да»/«ок».
MIN_SPEECH_SECONDS = 0.20

# Пресеты «агрессивности» вырезания тишины. Чем агрессивнее — тем выше порог
# вероятности речи (threshold), короче требуемая пауза для разреза
# (min_silence_duration_ms) и меньше паддинг вокруг речи (speech_pad_ms).
# Дефолт — "medium". На "low" VAD максимально бережный к тихой речи.
_AGGRESSIVENESS_PRESETS: dict[str, dict[str, float]] = {
    "low": {
        "threshold": 0.30,
        "min_silence_duration_ms": 700,
        "speech_pad_ms": 400,
        "min_speech_duration_ms": 120,
    },
    "medium": {
        "threshold": 0.50,
        "min_silence_duration_ms": 500,
        "speech_pad_ms": 300,
        "min_speech_duration_ms": 200,
    },
    "high": {
        "threshold": 0.60,
        "min_silence_duration_ms": 350,
        "speech_pad_ms": 200,
        "min_speech_duration_ms": 250,
    },
}
DEFAULT_AGGRESSIVENESS = "medium"


@dataclass(frozen=True)
class TrimResult:
    """Результат пре-обрезки тишины для облачной диктовки.

    wav_path      — путь к WAV для отправки (обрезанный при trimmed=True,
                    иначе не имеет смысла: см. no_speech).
    trimmed       — True, если построен speech-only WAV (нужна очистка через
                    cleanup_trim_artifact).
    no_speech     — True, если речи не найдено (или её меньше MIN_SPEECH_SECONDS):
                    вызывающий НЕ идёт в облако и показывает «Речь не найдена».
    speech_seconds — суммарная длительность найденной речи (для логов).
    """

    wav_path: Path | None
    trimmed: bool
    no_speech: bool
    speech_seconds: float


def normalize_aggressiveness(value: str | None) -> str:
    v = (value or "").strip().lower()
    return v if v in _AGGRESSIVENESS_PRESETS else DEFAULT_AGGRESSIVENESS


def _build_vad_options(aggressiveness: str):  # noqa: ANN001
    """Собрать faster_whisper.vad.VadOptions из пресета агрессивности."""
    from faster_whisper.vad import VadOptions

    preset = _AGGRESSIVENESS_PRESETS[normalize_aggressiveness(aggressiveness)]
    return VadOptions(
        threshold=float(preset["threshold"]),
        min_speech_duration_ms=int(preset["min_speech_duration_ms"]),
        min_silence_duration_ms=int(preset["min_silence_duration_ms"]),
        speech_pad_ms=int(preset["speech_pad_ms"]),
    )


def _read_wav_int16_mono_16k(wav_path: Path) -> np.ndarray:
    """Прочитать WAV (ожидается 16 кГц mono s16) в массив int16."""
    with wave.open(str(wav_path), "rb") as src:
        if src.getframerate() != VAD_SAMPLE_RATE or src.getnchannels() != 1 or src.getsampwidth() != 2:
            raise ValueError(
                f"Ожидался WAV 16кГц/mono/s16, получено "
                f"rate={src.getframerate()} ch={src.getnchannels()} width={src.getsampwidth()}"
            )
        frames = src.readframes(src.getnframes())
    return np.frombuffer(frames, dtype=np.int16)


def _merge_overlapping(segments: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Слить пересекающиеся/смежные интервалы (в сэмплах).

    speech_pad_ms может привести к перекрытию соседних сегментов — при склейке
    это дало бы дублирование звука. Сливаем такие интервалы.
    """
    if not segments:
        return []
    ordered = sorted(segments)
    merged: list[list[int]] = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


def detect_speech_segments(
    audio_f32: np.ndarray,
    *,
    sample_rate: int = VAD_SAMPLE_RATE,
    aggressiveness: str = DEFAULT_AGGRESSIVENESS,
) -> list[tuple[int, int]]:
    """Найти интервалы речи (в сэмплах) через Silero VAD faster-whisper.

    Возвращает список (start_sample, end_sample) с уже применённым speech_pad,
    слитый по перекрытиям. Пустой список = речь не найдена.
    """
    from faster_whisper.vad import get_speech_timestamps

    if audio_f32.dtype != np.float32:
        audio_f32 = audio_f32.astype(np.float32)
    options = _build_vad_options(aggressiveness)
    raw = get_speech_timestamps(audio_f32, vad_options=options, sampling_rate=sample_rate)
    segments = [
        (int(item["start"]), int(item["end"]))
        for item in raw
        if int(item.get("end", 0)) > int(item.get("start", 0))
    ]
    return _merge_overlapping(segments)


def _write_speech_only_wav(samples_int16: np.ndarray, segments: list[tuple[int, int]]) -> Path:
    """Собрать speech-only WAV (16кГц/mono/s16) из интервалов речи."""
    total = samples_int16.shape[0]
    pieces = [
        samples_int16[max(0, s):min(total, e)]
        for s, e in segments
        if min(total, e) > max(0, s)
    ]
    speech = np.concatenate(pieces) if pieces else np.empty(0, dtype=np.int16)
    out_dir = Path(tempfile.mkdtemp(prefix="voice-input-vad-"))
    out_path = out_dir / "speech-only-16k-mono.wav"
    with wave.open(str(out_path), "wb") as dst:
        dst.setnchannels(1)
        dst.setsampwidth(2)
        dst.setframerate(VAD_SAMPLE_RATE)
        dst.writeframes(speech.tobytes())
    return out_path


def trim_silence_for_cloud(src_wav: Path, *, aggressiveness: str = DEFAULT_AGGRESSIVENESS) -> TrimResult:
    """Вырезать тишину из записи диктовки перед отправкой в облачный STT.

    Три случая (см. US-039, TASK-203):
      1. речь найдена (≥ MIN_SPEECH_SECONDS) → построить speech-only WAV
         (trimmed=True, no_speech=False);
      2. речи нет / её меньше порога → no_speech=True (в облако не идём);
      3. любой сбой (нет faster-whisper, битый звук, исключение VAD) — НЕ
         обрабатывается здесь: исключение пробрасывается наверх, где вызывающий
         делает fail-open (отправляет оригинал без обрезки). Это разграничение
         намеренно: «нет речи» ≠ «VAD сломался».

    Исходный WAV может быть на частоте устройства (напр. 48кГц) — приводим к
    16кГц mono через PyAV (convert_media_to_wav_16k_mono), заодно уменьшая
    payload для облака.
    """
    prepared_path, _duration = convert_media_to_wav_16k_mono(src_wav)
    try:
        samples = _read_wav_int16_mono_16k(prepared_path)
        audio_f32 = samples.astype(np.float32) / 32768.0
        segments = detect_speech_segments(audio_f32, aggressiveness=aggressiveness)
    finally:
        # Промежуточный 16к-WAV больше не нужен (обрезанный строим из уже
        # прочитанных сэмплов).
        cleanup_prepared_file(prepared_path)

    speech_samples = sum(max(0, e - s) for s, e in segments)
    speech_seconds = speech_samples / float(VAD_SAMPLE_RATE)
    if not segments or speech_seconds < MIN_SPEECH_SECONDS:
        log.info("VAD: речь не найдена (%.3fс речи, порог %.2fс)", speech_seconds, MIN_SPEECH_SECONDS)
        return TrimResult(wav_path=None, trimmed=False, no_speech=True, speech_seconds=speech_seconds)

    out_path = _write_speech_only_wav(samples, segments)
    log.info(
        "VAD: оставлено %.3fс речи в %d сегментах (агрессивность=%s) → %s",
        speech_seconds, len(segments), normalize_aggressiveness(aggressiveness), out_path,
    )
    return TrimResult(wav_path=out_path, trimmed=True, no_speech=False, speech_seconds=speech_seconds)


def cleanup_trim_artifact(path: Path | None) -> None:
    """Удалить обрезанный WAV и его временную папку (вызывает вызывающий)."""
    if path is None:
        return
    cleanup_prepared_file(path)
    try:
        parent = path.parent
        if parent.name.startswith("voice-input-vad-"):
            parent.rmdir()
    except Exception:  # noqa: BLE001
        pass
