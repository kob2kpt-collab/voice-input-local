from __future__ import annotations

import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

from .logger import get_logger

log = get_logger("audio_files")

SUPPORTED_AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".mp4", ".webm", ".ogg", ".flac", ".aac", ".wma", ".mov", ".mkv"}


@dataclass(frozen=True)
class AudioChunk:
    path: Path
    start_seconds: float
    end_seconds: float


def format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def is_supported_audio_file(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS


def get_media_duration_seconds(path: Path) -> float:
    """Return media duration using PyAV, with a WAV fallback."""
    try:
        import av

        with av.open(str(path)) as container:
            audio_streams = [stream for stream in container.streams if stream.type == "audio"]
            if not audio_streams:
                raise RuntimeError("В файле не найдена аудиодорожка.")
            stream = audio_streams[0]
            if stream.duration is not None and stream.time_base is not None:
                duration = float(stream.duration * stream.time_base)
                if duration > 0:
                    return duration
            if container.duration is not None:
                duration = float(container.duration) / 1_000_000.0
                if duration > 0:
                    return duration
    except Exception as exc:  # noqa: BLE001
        log.warning("PyAV duration probe failed for %s: %s", path, exc)

    if path.suffix.lower() == ".wav":
        with wave.open(str(path), "rb") as src:
            frames = src.getnframes()
            rate = src.getframerate()
            if rate > 0:
                return frames / float(rate)
    return 0.0


def convert_media_to_wav_16k_mono(path: Path) -> tuple[Path, float]:
    """Decode the first audio stream and write a temporary 16 kHz mono WAV."""
    try:
        import av
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "Не удалось подготовить аудиофайл: библиотека PyAV недоступна. "
            "Запустите install.bat ещё раз или установите faster-whisper/PyAV."
        ) from exc

    out_dir = Path(tempfile.mkdtemp(prefix="voice-input-file-"))
    out_path = out_dir / (path.stem[:40] + "-16k-mono.wav")
    total_samples = 0

    with av.open(str(path)) as container:
        audio_streams = [stream for stream in container.streams if stream.type == "audio"]
        if not audio_streams:
            raise RuntimeError("В файле не найдена аудиодорожка.")
        stream = audio_streams[0]
        resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=16000)
        with wave.open(str(out_path), "wb") as dst:
            dst.setnchannels(1)
            dst.setsampwidth(2)
            dst.setframerate(16000)
            for frame in container.decode(stream):
                frames = resampler.resample(frame)
                if frames is None:
                    continue
                if not isinstance(frames, list):
                    frames = [frames]
                for resampled in frames:
                    if resampled is None:
                        continue
                    dst.writeframes(resampled.to_ndarray().tobytes())
                    total_samples += int(getattr(resampled, "samples", 0) or 0)
            try:
                tail_frames = resampler.resample(None)
            except Exception:
                tail_frames = []
            if tail_frames is None:
                tail_frames = []
            if not isinstance(tail_frames, list):
                tail_frames = [tail_frames]
            for resampled in tail_frames:
                if resampled is None:
                    continue
                dst.writeframes(resampled.to_ndarray().tobytes())
                total_samples += int(getattr(resampled, "samples", 0) or 0)

    if not out_path.exists() or out_path.stat().st_size <= 44:
        raise RuntimeError("Не удалось подготовить WAV: после декодирования файл пустой.")
    duration = total_samples / 16000.0 if total_samples > 0 else get_media_duration_seconds(out_path)
    return out_path, duration


def split_wav_by_duration(wav_path: Path, chunk_seconds: float = 24.0, overlap_seconds: float = 0.0) -> list[AudioChunk]:
    """Split a WAV into chunk files and keep their timeline positions."""
    with wave.open(str(wav_path), "rb") as src:
        channels = src.getnchannels()
        sampwidth = src.getsampwidth()
        framerate = src.getframerate()
        frames = src.getnframes()
        if frames <= 0 or framerate <= 0:
            return []
        duration = frames / float(framerate)
        if duration <= chunk_seconds:
            return [AudioChunk(wav_path, 0.0, duration)]
        bytes_per_frame = channels * sampwidth
        chunk_frames = max(1, int(chunk_seconds * framerate))
        overlap_frames = int(max(0.0, min(overlap_seconds, chunk_seconds / 2.0)) * framerate)
        step_frames = max(1, chunk_frames - overlap_frames)
        data = src.readframes(frames)

    tmp_dir = Path(tempfile.mkdtemp(prefix="voice-input-file-chunks-"))
    chunks: list[AudioChunk] = []
    start_frame = 0
    index = 0
    while start_frame < frames:
        index += 1
        end_frame = min(frames, start_frame + chunk_frames)
        start_byte = start_frame * bytes_per_frame
        end_byte = end_frame * bytes_per_frame
        out_path = tmp_dir / f"file-chunk-{index:04d}.wav"
        with wave.open(str(out_path), "wb") as dst:
            dst.setnchannels(channels)
            dst.setsampwidth(sampwidth)
            dst.setframerate(framerate)
            dst.writeframes(data[start_byte:end_byte])
        chunks.append(AudioChunk(out_path, start_frame / float(framerate), end_frame / float(framerate)))
        if end_frame >= frames:
            break
        start_frame += step_frames
    return chunks


def cleanup_prepared_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass
    try:
        parent = path.parent
        if parent.name.startswith("voice-input-file-") or parent.name.startswith("voice-input-file-chunks-"):
            parent.rmdir()
    except Exception:
        pass
