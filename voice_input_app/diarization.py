from __future__ import annotations

import math
import wave
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np

from .models import TranscriptSegment
from .logger import get_logger

log = get_logger("diarization")


@dataclass(frozen=True)
class SpeakerInterval:
    start_seconds: float
    end_seconds: float
    speaker: str


@dataclass(frozen=True)
class SpeakerTimeline:
    intervals: tuple[SpeakerInterval, ...]
    speaker_count: int
    window_count: int
    duration_seconds: float


def _read_wav_float32(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as src:
        channels = src.getnchannels()
        rate = src.getframerate()
        width = src.getsampwidth()
        frames = src.getnframes()
        data = src.readframes(frames)
    if width == 2:
        audio = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
    elif width == 4:
        audio = np.frombuffer(data, dtype=np.float32).astype(np.float32)
    else:
        audio = np.frombuffer(data, dtype=np.uint8).astype(np.float32)
        audio = (audio - 128.0) / 128.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return audio, rate


def _features(y: np.ndarray, rate: int) -> np.ndarray:
    if y.size == 0:
        return np.zeros(12, dtype=np.float32)
    y = y.astype(np.float32)
    if y.size > rate * 8:
        y = y[: rate * 8]
    rms = float(np.sqrt(np.mean(np.square(y)) + 1e-9))
    zcr = float(np.mean(np.abs(np.diff(np.signbit(y)).astype(np.float32)))) if y.size > 1 else 0.0
    win = np.hanning(len(y)).astype(np.float32) if len(y) > 8 else np.ones_like(y)
    spec = np.abs(np.fft.rfft(y * win)) + 1e-9
    freqs = np.fft.rfftfreq(len(y), d=1.0 / rate)
    total = float(np.sum(spec)) + 1e-9
    norm_freqs = freqs / max(1.0, rate / 2.0)
    centroid = float(np.sum(norm_freqs * spec) / total)
    spread = float(np.sqrt(np.sum(((norm_freqs - centroid) ** 2) * spec) / total))
    cumsum = np.cumsum(spec)
    rolloff_idx = int(np.searchsorted(cumsum, cumsum[-1] * 0.85))
    rolloff = float(norm_freqs[min(rolloff_idx, len(norm_freqs) - 1)])
    bands = []
    for lo, hi in [(80, 250), (250, 500), (500, 1000), (1000, 2000), (2000, 3500), (3500, 6000)]:
        bands.append(float(np.sum(spec[(freqs >= lo) & (freqs < hi)]) / total))
    third = max(1, len(spec) // 3)
    low_energy = float(np.sum(spec[:third]) / total)
    high_energy = float(np.sum(spec[-third:]) / total)
    log_rms = math.log(rms + 1e-6)
    return np.array([log_rms, zcr, centroid, spread, rolloff, low_energy, high_energy, *bands], dtype=np.float32)


def _speaker_count_from_setting(value: str, feature_count: int, duration: float) -> int:
    if value in {"2", "3", "4"}:
        return min(int(value), max(1, feature_count))
    if feature_count < 4 or duration < 12:
        return 1
    if duration < 45 or feature_count < 18:
        return 2
    return min(4, max(2, int(round(math.sqrt(feature_count / 4.0)))))


def _kmeans(features: np.ndarray, k: int, iterations: int = 35) -> np.ndarray:
    if len(features) == 0:
        return np.array([], dtype=np.int32)
    if k <= 1:
        return np.zeros(len(features), dtype=np.int32)
    mean = features.mean(axis=0)
    std = features.std(axis=0) + 1e-6
    x = (features - mean) / std
    centers = [x[0]]
    while len(centers) < k:
        d = np.min(np.stack([np.sum((x - c) ** 2, axis=1) for c in centers]), axis=0)
        centers.append(x[int(np.argmax(d))])
    centers = np.stack(centers)
    labels = np.zeros(len(x), dtype=np.int32)
    for _ in range(iterations):
        dist = np.stack([np.sum((x - c) ** 2, axis=1) for c in centers], axis=1)
        new_labels = dist.argmin(axis=1).astype(np.int32)
        if np.array_equal(new_labels, labels):
            break
        labels = new_labels
        for idx in range(k):
            mask = labels == idx
            if np.any(mask):
                centers[idx] = x[mask].mean(axis=0)
    return labels


def _smooth(labels: np.ndarray, passes: int = 2) -> np.ndarray:
    if labels.size < 3:
        return labels
    out = labels.copy()
    for _ in range(passes):
        for i in range(1, len(out) - 1):
            if out[i - 1] == out[i + 1] and out[i] != out[i - 1]:
                out[i] = out[i - 1]
    return out


def _build_windows(audio: np.ndarray, rate: int, *, win_s: float = 1.8, hop_s: float = 0.9) -> tuple[list[tuple[float, float]], np.ndarray]:
    win = max(1, int(win_s * rate))
    hop = max(1, int(hop_s * rate))
    if audio.size <= 0:
        return [], np.zeros((0, 12), dtype=np.float32)
    rms_all: list[float] = []
    starts = list(range(0, max(1, audio.size - win + 1), hop))
    if not starts or starts[-1] + win < audio.size:
        starts.append(max(0, audio.size - win))
    for s in starts:
        y = audio[s : min(audio.size, s + win)]
        rms_all.append(float(np.sqrt(np.mean(np.square(y)) + 1e-9)))
    noise_floor = float(np.percentile(rms_all, 25)) if rms_all else 0.0
    threshold = max(noise_floor * 1.7, 0.0025)
    windows: list[tuple[float, float]] = []
    feats: list[np.ndarray] = []
    for s, rms in zip(starts, rms_all):
        y = audio[s : min(audio.size, s + win)]
        if rms < threshold and len(feats) > 0:
            continue
        start = s / float(rate)
        end = min(audio.size, s + win) / float(rate)
        windows.append((start, end))
        feats.append(_features(y, rate))
    if not feats:
        for s in starts[: max(1, min(8, len(starts)))]:
            y = audio[s : min(audio.size, s + win)]
            windows.append((s / float(rate), min(audio.size, s + win) / float(rate)))
            feats.append(_features(y, rate))
    return windows, np.stack(feats) if feats else np.zeros((0, 12), dtype=np.float32)


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _merge_label_windows(windows: Sequence[tuple[float, float]], labels: np.ndarray) -> list[SpeakerInterval]:
    intervals: list[SpeakerInterval] = []
    current_label: int | None = None
    current_start = 0.0
    current_end = 0.0
    for (start, end), label_value in zip(windows, labels):
        label = int(label_value)
        if current_label is None:
            current_label = label
            current_start = float(start)
            current_end = float(end)
            continue
        if label == current_label and start <= current_end + 0.35:
            current_end = max(current_end, float(end))
        else:
            intervals.append(SpeakerInterval(current_start, current_end, f"Спикер {current_label + 1}"))
            current_label = label
            current_start = float(start)
            current_end = float(end)
    if current_label is not None:
        intervals.append(SpeakerInterval(current_start, current_end, f"Спикер {current_label + 1}"))
    return intervals


def build_speaker_timeline(wav_path: Path, *, speaker_count: str = "auto") -> SpeakerTimeline:
    """Build a speaker timeline before ASR emits UI blocks.

    This mirrors the Vibe UX: every emitted segment can already carry a speaker
    label. The current runtime is lightweight local acoustic clustering, isolated
    behind this function so a dedicated Sortformer ONNX runtime can replace it.
    """
    audio, rate = _read_wav_float32(wav_path)
    duration = len(audio) / float(rate) if rate else 0.0
    windows, matrix = _build_windows(audio, rate)
    k = _speaker_count_from_setting(str(speaker_count or "auto"), len(windows), duration)
    labels = _smooth(_kmeans(matrix, k))
    intervals = _merge_label_windows(windows, labels)
    speakers = sorted(set(interval.speaker for interval in intervals))
    log.info(
        "Diarization timeline ready: speakers=%s intervals=%s windows=%s duration=%.2f requested=%s",
        len(speakers) or k,
        len(intervals),
        len(windows),
        duration,
        speaker_count,
    )
    return SpeakerTimeline(tuple(intervals), len(speakers) or k, len(windows), duration)


def speaker_for_interval(timeline: SpeakerTimeline | None, start_seconds: float, end_seconds: float) -> str:
    if timeline is None or not timeline.intervals:
        return ""
    scores: Counter[str] = Counter()
    for interval in timeline.intervals:
        ov = _overlap(start_seconds, end_seconds, interval.start_seconds, interval.end_seconds)
        if ov > 0:
            scores[interval.speaker] += ov
    if scores:
        return scores.most_common(1)[0][0]
    center = (start_seconds + end_seconds) / 2.0
    nearest = min(timeline.intervals, key=lambda item: abs(((item.start_seconds + item.end_seconds) / 2.0) - center))
    return nearest.speaker


def assign_speakers(wav_path: Path, blocks: Sequence[TranscriptSegment], *, speaker_count: str = "auto") -> list[TranscriptSegment]:
    """Assign segment-level speaker labels using the local speaker timeline."""
    if not blocks:
        return []
    timeline = build_speaker_timeline(wav_path, speaker_count=speaker_count)
    result: list[TranscriptSegment] = []
    for block in blocks:
        speaker = speaker_for_interval(timeline, block.start_seconds, block.end_seconds)
        result.append(TranscriptSegment(block.start_seconds, block.end_seconds, block.text, speaker))
    log.info(
        "Diarization assigned speakers=%s blocks=%s windows=%s duration=%.2f requested=%s",
        timeline.speaker_count,
        len(result),
        timeline.window_count,
        timeline.duration_seconds,
        speaker_count,
    )
    return result
