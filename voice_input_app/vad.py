"""VAD-обёртка для вырезания тишины перед отправкой звука в облачный STT.

EPIC-10 / US-039 / US-040. Цель — убрать галлюцинации Whisper на участках
тишины и в паузах: облачные модели (в отличие от локального faster-whisper с
vad_filter) получают сырой звук и «додумывают» фразы в паузах. Здесь мы локально
находим речь и отправляем в облако только её.

US-040: VAD ещё и задаёт границы нарезки на чанки. Раньше обрезанный звук резался
жёсткой сеткой (i × chunk), из-за чего граница падала внутри слова и на стыке
возникал дубль (перехлёст склеивался дважды). Теперь сегменты речи группируются
в корзины ≤ chunk, а разрез ставится ТОЛЬКО на паузе между фразами (аналог
переноса слов по пробелам). VAD и чанкинг — одна операция, не две конкурирующие.

Движок — Silero VAD, встроенный в faster-whisper (faster_whisper.vad). Он не
требует загруженной модели Whisper и не создаёт ресурсного конфликта с
локальной расшифровкой (разные рантаймы: CTranslate2 vs ONNX Runtime; ORT
InferenceSession потокобезопасен). Ноль новых зависимостей — onnxruntime уже
присутствует транзитивно.

Абстракция намеренно тонкая: если внутренний API faster_whisper.vad окажется
нестабильным между версиями, достаточно переписать detect_speech_segments()
на пакет silero-vad, не трогая вызовы выше (workers.py) и UI.

Применяется ТОЛЬКО к облачной диктовке (без таймкодов). Файловый путь
(таймкоды/диаризация) здесь не затрагивается.
"""

from __future__ import annotations

import shutil
import tempfile
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .audio_files import AudioChunk, cleanup_prepared_file, convert_media_to_wav_16k_mono
from .logger import get_logger

log = get_logger("vad")

# Частота, на которой работает Silero VAD и в которой мы отдаём обрезанный звук.
VAD_SAMPLE_RATE = 16000

# Минимальная суммарная длительность речи, ниже которой считаем, что речи нет
# (случай «тишина» — спурьёзные щелчки/шум). Возвращаем no_speech, в облако не
# идём. Порог намеренно мал, чтобы не терять короткие реплики вроде «да»/«ок».
MIN_SPEECH_SECONDS = 0.20

# US-040: перехлёст на случай жёсткого разреза ВНУТРИ непрерывного сегмента,
# который длиннее размера чанка (пауз нет — резать больше негде). Для обычных
# разрезов по паузам перехлёст не нужен (режем в тишине).
HARDCUT_OVERLAP_SECONDS = 0.3

# «Агрессивность» вырезания тишины — непрерывный уровень 0..100 (ползунок в UI).
# 0 = максимально бережно к тихой речи (режем меньше), 100 = агрессивно режем
# паузы. Уровень линейно интерполируется в параметры Silero VAD. 50 ≈ прежний
# пресет «средняя». Чем выше уровень — тем выше порог вероятности речи
# (threshold), короче требуемая пауза (min_silence) и меньше паддинг (speech_pad).
AGGRESSIVENESS_MIN = 0
AGGRESSIVENESS_MAX = 100
DEFAULT_AGGRESSIVENESS = 50

# Границы интерполяции: (значение при уровне 0 — бережно, значение при 100 — агрессивно).
_AGG_ENDPOINTS: dict[str, tuple[float, float]] = {
    "threshold": (0.25, 0.70),
    "min_silence_duration_ms": (800.0, 250.0),
    "speech_pad_ms": (500.0, 100.0),
    "min_speech_duration_ms": (100.0, 300.0),
}
# Обратная совместимость: старые строковые пресеты (config < v4.14 ползунка) → уровень.
_LEGACY_LEVELS = {"low": 20, "medium": 50, "high": 80}


@dataclass(frozen=True)
class TrimResult:
    """Результат пре-обрезки тишины для облачной диктовки.

    wav_path      — полный speech-only WAV (склейка всей речи без тишины). Идёт
                    в облако при одном чанке и используется локальным fallback.
    chunks        — список AudioChunk (≥1) для параллельной отправки в облако.
                    Границы поставлены по паузам (US-040). При >1 элементе caller
                    прокидывает их как precut_chunks в split_and_transcribe.
    trimmed       — True, если построен speech-only WAV (нужна очистка через
                    cleanup_trim_artifact(temp_dir)).
    no_speech     — True, если речи не найдено (или её меньше MIN_SPEECH_SECONDS):
                    вызывающий НЕ идёт в облако и показывает «Речь не найдена».
    speech_seconds — суммарная длительность найденной речи (для логов).
    temp_dir      — папка со всеми артефактами (wav_path + chunk-файлы); очищается
                    целиком через cleanup_trim_artifact.
    """

    wav_path: Path | None
    chunks: list = field(default_factory=list)
    trimmed: bool = False
    no_speech: bool = False
    speech_seconds: float = 0.0
    temp_dir: Path | None = None


def normalize_aggressiveness(value) -> int:  # noqa: ANN001
    """Привести значение агрессивности к целому 0..100.

    Принимает int/float, числовую строку или старые пресеты low/medium/high
    (обратная совместимость со старым config.json).
    """
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _LEGACY_LEVELS:
            return _LEGACY_LEVELS[v]
        try:
            value = float(v)
        except ValueError:
            return DEFAULT_AGGRESSIVENESS
    try:
        level = int(round(float(value)))
    except (TypeError, ValueError):
        return DEFAULT_AGGRESSIVENESS
    return max(AGGRESSIVENESS_MIN, min(AGGRESSIVENESS_MAX, level))


def _build_vad_options(aggressiveness):  # noqa: ANN001
    """Собрать faster_whisper.vad.VadOptions по уровню агрессивности 0..100."""
    from faster_whisper.vad import VadOptions

    t = normalize_aggressiveness(aggressiveness) / 100.0

    def _lerp(pair: tuple[float, float]) -> float:
        a, b = pair
        return a + (b - a) * t

    return VadOptions(
        threshold=float(_lerp(_AGG_ENDPOINTS["threshold"])),
        min_speech_duration_ms=int(round(_lerp(_AGG_ENDPOINTS["min_speech_duration_ms"]))),
        min_silence_duration_ms=int(round(_lerp(_AGG_ENDPOINTS["min_silence_duration_ms"]))),
        speech_pad_ms=int(round(_lerp(_AGG_ENDPOINTS["speech_pad_ms"]))),
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
    aggressiveness: int = DEFAULT_AGGRESSIVENESS,
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


def _group_segments_into_buckets(
    segments: list[tuple[int, int]],
    *,
    sample_rate: int,
    max_chunk_seconds: float,
) -> list[list[tuple[int, int]]]:
    """US-040: сгруппировать сегменты речи в корзины ≤ max_chunk (в сэмплах).

    Разрез между корзинами ставится на паузе (границе сегмента). Единственный
    краевой случай — непрерывный сегмент длиннее размера чанка (пауз нет): режем
    его жёстко на куски ≤ max_chunk с перехлёстом HARDCUT_OVERLAP_SECONDS.
    Каждая корзина — список сегментов (для одиночной корзины при hard-cut это
    один синтетический под-сегмент).
    """
    max_s = int(max_chunk_seconds * sample_rate)
    if max_s <= 0:
        return [list(segments)] if segments else []
    overlap = int(HARDCUT_OVERLAP_SECONDS * sample_rate)
    buckets: list[list[tuple[int, int]]] = []
    current: list[tuple[int, int]] = []
    current_dur = 0
    for start, end in segments:
        seg_dur = end - start
        if seg_dur > max_s:
            # Непрерывная речь длиннее чанка — резать по паузам негде.
            if current:
                buckets.append(current)
                current, current_dur = [], 0
            pos = start
            while pos < end:
                piece_end = min(end, pos + max_s)
                buckets.append([(pos, piece_end)])
                if piece_end >= end:
                    break
                pos = max(pos + 1, piece_end - overlap)
            continue
        if current and current_dur + seg_dur > max_s:
            buckets.append(current)
            current, current_dur = [], 0
        current.append((start, end))
        current_dur += seg_dur
    if current:
        buckets.append(current)
    return buckets


def _concat_segments(samples_int16: np.ndarray, bucket: list[tuple[int, int]]) -> np.ndarray:
    """Склеить сэмплы речи по списку интервалов (в сэмплах)."""
    total = samples_int16.shape[0]
    pieces = [
        samples_int16[max(0, s):min(total, e)]
        for s, e in bucket
        if min(total, e) > max(0, s)
    ]
    return np.concatenate(pieces) if pieces else np.empty(0, dtype=np.int16)


def _write_wav(out_path: Path, samples_int16: np.ndarray) -> None:
    with wave.open(str(out_path), "wb") as dst:
        dst.setnchannels(1)
        dst.setsampwidth(2)
        dst.setframerate(VAD_SAMPLE_RATE)
        dst.writeframes(samples_int16.tobytes())


def trim_silence_for_cloud(
    src_wav: Path,
    *,
    aggressiveness: int = DEFAULT_AGGRESSIVENESS,
    max_chunk_seconds: float = 0.0,
) -> TrimResult:
    """Вырезать тишину из записи диктовки перед отправкой в облачный STT.

    Три случая (US-039, TASK-203):
      1. речь найдена (≥ MIN_SPEECH_SECONDS) → построить speech-only WAV;
      2. речи нет / её меньше порога → no_speech=True (в облако не идём);
      3. любой сбой (нет faster-whisper, битый звук, исключение VAD) — НЕ
         обрабатывается здесь: исключение пробрасывается наверх, где вызывающий
         делает fail-open (отправляет оригинал без обрезки).

    US-040: при max_chunk_seconds > 0 речь дополнительно разбивается на корзины
    ≤ max_chunk по паузам (chunks). При одном чанке chunks = [полный WAV].

    Исходный WAV может быть на частоте устройства (напр. 48кГц) — приводим к
    16кГц mono через PyAV, заодно уменьшая payload для облака.
    """
    prepared_path, _duration = convert_media_to_wav_16k_mono(src_wav)
    try:
        samples = _read_wav_int16_mono_16k(prepared_path)
        audio_f32 = samples.astype(np.float32) / 32768.0
        segments = detect_speech_segments(audio_f32, aggressiveness=aggressiveness)
    finally:
        cleanup_prepared_file(prepared_path)

    speech_samples = sum(max(0, e - s) for s, e in segments)
    speech_seconds = speech_samples / float(VAD_SAMPLE_RATE)
    if not segments or speech_seconds < MIN_SPEECH_SECONDS:
        log.info("VAD: речь не найдена (%.3fс речи, порог %.2fс)", speech_seconds, MIN_SPEECH_SECONDS)
        return TrimResult(wav_path=None, chunks=[], trimmed=False, no_speech=True, speech_seconds=speech_seconds)

    out_dir = Path(tempfile.mkdtemp(prefix="voice-input-vad-"))
    # Полный speech-only WAV — для одиночного запроса и локального fallback.
    full_samples = _concat_segments(samples, segments)
    full_path = out_dir / "speech-full-16k-mono.wav"
    _write_wav(full_path, full_samples)

    buckets = _group_segments_into_buckets(segments, sample_rate=VAD_SAMPLE_RATE, max_chunk_seconds=max_chunk_seconds)
    if len(buckets) <= 1:
        # Один чанк — переиспользуем полный WAV, не плодим файлы.
        chunks = [AudioChunk(full_path, 0.0, speech_seconds)]
    else:
        chunks = []
        offset = 0.0
        for index, bucket in enumerate(buckets, start=1):
            piece = _concat_segments(samples, bucket)
            piece_path = out_dir / f"speech-chunk-{index:04d}.wav"
            _write_wav(piece_path, piece)
            dur = piece.shape[0] / float(VAD_SAMPLE_RATE)
            chunks.append(AudioChunk(piece_path, offset, offset + dur))
            offset += dur

    log.info(
        "VAD: оставлено %.3fс речи в %d сегментах → %d чанк(ов) (агрессивность=%s, chunk≤%.0fс)",
        speech_seconds, len(segments), len(chunks), normalize_aggressiveness(aggressiveness), max_chunk_seconds,
    )
    return TrimResult(
        wav_path=full_path,
        chunks=chunks,
        trimmed=True,
        no_speech=False,
        speech_seconds=speech_seconds,
        temp_dir=out_dir,
    )


def cleanup_trim_artifact(path: Path | None) -> None:
    """Удалить временную папку VAD со всеми артефактами (вызывает вызывающий).

    Принимает temp_dir из TrimResult. Для обратной совместимости так же
    корректно обрабатывает путь к файлу внутри такой папки.
    """
    if path is None:
        return
    target = path if path.is_dir() else path.parent
    try:
        if target.name.startswith("voice-input-vad-"):
            shutil.rmtree(target, ignore_errors=True)
        elif path.is_file():
            cleanup_prepared_file(path)
    except Exception:  # noqa: BLE001
        pass
