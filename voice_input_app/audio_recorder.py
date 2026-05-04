from __future__ import annotations

import time
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import sounddevice as sd

from .logger import get_logger
from .paths import recordings_dir

log = get_logger("audio")


@dataclass(frozen=True)
class AudioInputDevice:
    device_id: str
    label: str
    index: int
    name: str
    hostapi_index: int
    hostapi_name: str
    default_samplerate: int
    max_input_channels: int


@dataclass(frozen=True)
class AudioOpenCandidate:
    device_index: int | None
    label: str
    hostapi_name: str
    preferred_rates: tuple[int, ...]
    use_wasapi_shared: bool


def _device_id(hostapi_index: int, name: str) -> str:
    return f"{hostapi_index}::{name}"


def _hostapi_names() -> dict[int, str]:
    try:
        hostapis = sd.query_hostapis()
        return {idx: str(api.get("name") or f"Host API {idx}") for idx, api in enumerate(hostapis)}
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not query PortAudio host APIs: %s", exc)
        return {}


def _api_rank(name: str) -> int:
    lowered = name.lower()
    if "wasapi" in lowered:
        return 0
    if "directsound" in lowered:
        return 1
    if "mme" in lowered:
        return 2
    # WDM-KS is kept as a late fallback only. It can behave less friendly
    # with OBS, softphones and other apps that already use audio devices.
    if "wdm-ks" in lowered or "wdm" in lowered:
        return 3
    return 4


def _default_input_device_index_for_hostapi(hostapi_index: int) -> int | None:
    try:
        hostapis = sd.query_hostapis()
        if 0 <= hostapi_index < len(hostapis):
            idx = hostapis[hostapi_index].get("default_input_device")
            if isinstance(idx, int) and idx >= 0:
                return idx
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not resolve default input device for host API %s: %s", hostapi_index, exc)
    return None


def _default_wasapi_input_candidate(devices: list[AudioInputDevice]) -> AudioInputDevice | None:
    for device in devices:
        if "wasapi" not in device.hostapi_name.lower():
            continue
        default_idx = _default_input_device_index_for_hostapi(device.hostapi_index)
        if default_idx is not None and device.index == default_idx:
            return device
    for device in devices:
        if "wasapi" in device.hostapi_name.lower():
            return device
    return None


def list_input_devices() -> list[AudioInputDevice]:
    """Return available input devices for the settings UI.

    The stored id intentionally uses hostapi+name instead of the volatile PortAudio
    device index. At recording time we resolve it back to the current index. Windows
    WASAPI devices are sorted first because shared-mode capture is usually the most
    compatible option when Zoom/Teams/Meet or a browser also uses the microphone.
    """
    devices: list[AudioInputDevice] = []
    hostapis = _hostapi_names()
    try:
        raw_devices = sd.query_devices()
    except Exception as exc:  # noqa: BLE001
        log.exception("Could not query audio input devices")
        raise RuntimeError(f"Не удалось получить список микрофонов: {exc}") from exc

    for index, info in enumerate(raw_devices):
        try:
            max_input_channels = int(info.get("max_input_channels") or 0)
        except Exception:
            max_input_channels = 0
        if max_input_channels <= 0:
            continue
        name = str(info.get("name") or f"Input {index}").strip()
        hostapi_index = int(info.get("hostapi") or 0)
        hostapi_name = hostapis.get(hostapi_index, f"Host API {hostapi_index}")
        try:
            default_samplerate = int(float(info.get("default_samplerate") or 0))
        except Exception:
            default_samplerate = 0
        label = f"{name} · {hostapi_name}"
        if default_samplerate > 0:
            label += f" · {default_samplerate} Hz"
        devices.append(
            AudioInputDevice(
                device_id=_device_id(hostapi_index, name),
                label=label,
                index=index,
                name=name,
                hostapi_index=hostapi_index,
                hostapi_name=hostapi_name,
                default_samplerate=default_samplerate,
                max_input_channels=max_input_channels,
            )
        )
    devices.sort(key=lambda d: (_api_rank(d.hostapi_name), d.name.lower(), d.index))
    return devices


def resolve_input_device(device_id: str | None) -> AudioInputDevice | None:
    if not device_id:
        return None
    for device in list_input_devices():
        if device.device_id == device_id:
            return device
    log.warning("Configured input device was not found, falling back to system default: %s", device_id)
    return None


class AudioRecorder:
    def __init__(self, sample_rate: int = 16000, input_device_id: str | None = None, meeting_compatibility: bool = True) -> None:
        self.requested_sample_rate = sample_rate
        self.input_device_id = input_device_id or ""
        self.meeting_compatibility = meeting_compatibility
        self.input_device: AudioInputDevice | None = None
        self.sample_rate = sample_rate
        self.channels = 1
        self._stream: sd.InputStream | None = None
        self._frames: list[np.ndarray] = []
        self._started_at: float | None = None
        self._active_label = ""

    @property
    def is_recording(self) -> bool:
        return self._stream is not None

    @property
    def elapsed(self) -> float:
        if self._started_at is None:
            return 0.0
        return max(0.0, time.perf_counter() - self._started_at)

    def _rate_candidates(self, preferred: tuple[int, ...] | list[int] | None = None) -> list[int]:
        rates: list[int] = []
        values: list[int] = []
        if preferred:
            values.extend(int(v) for v in preferred if int(v) > 0)
        # In meeting compatibility mode, default device rate comes before 16 kHz.
        # That avoids MME/WASAPI failures on microphones that are opened by other
        # meeting software at 44.1/48 kHz.
        if self.meeting_compatibility:
            values.extend([self.requested_sample_rate, 48000, 44100, 32000, 16000])
        else:
            values.extend([self.requested_sample_rate, 16000, 48000, 44100, 32000])
        for value in values:
            try:
                rate = int(value)
            except Exception:
                continue
            if rate > 0 and rate not in rates:
                rates.append(rate)
        return rates

    def _candidate_sample_rates(self, device_index: int | None) -> list[int]:
        preferred: list[int] = []
        try:
            if device_index is None:
                default_device = sd.query_devices(kind="input")
            else:
                default_device = sd.query_devices(device_index)
            default_rate = int(float(default_device.get("default_samplerate") or 0))
            if default_rate > 0:
                preferred.append(default_rate)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not query selected input device sample rate: %s", exc)
        return self._rate_candidates(preferred)

    def _candidate_devices(self) -> list[AudioOpenCandidate]:
        devices = list_input_devices()
        selected = resolve_input_device(self.input_device_id)
        self.input_device = selected
        candidates: list[AudioOpenCandidate] = []
        seen: set[tuple[int | None, str]] = set()

        def add(device: AudioInputDevice | None, label: str | None = None) -> None:
            if device is None:
                key = (None, "default")
                if key in seen:
                    return
                seen.add(key)
                candidates.append(
                    AudioOpenCandidate(
                        device_index=None,
                        label=label or "Системный микрофон по умолчанию",
                        hostapi_name="default",
                        preferred_rates=tuple(self._candidate_sample_rates(None)),
                        use_wasapi_shared=False,
                    )
                )
                return
            key = (device.index, device.hostapi_name)
            if key in seen:
                return
            seen.add(key)
            candidates.append(
                AudioOpenCandidate(
                    device_index=device.index,
                    label=label or device.label,
                    hostapi_name=device.hostapi_name,
                    preferred_rates=tuple(self._candidate_sample_rates(device.index)),
                    use_wasapi_shared="wasapi" in device.hostapi_name.lower(),
                )
            )

        if selected is not None:
            # If the user selected a concrete microphone, do not probe unrelated
            # devices. OBS/softphones may legitimately own another microphone,
            # and touching it as a fallback can make dictation fail even though
            # the selected microphone is available.
            same_physical_device = [d for d in devices if d.name == selected.name]
            same_physical_device.sort(key=lambda d: (_api_rank(d.hostapi_name), d.index))

            if self.meeting_compatibility:
                for device in same_physical_device:
                    if "wasapi" in device.hostapi_name.lower():
                        add(device, f"{device.name} · Windows WASAPI shared")

            add(selected)

            for device in same_physical_device:
                if device.index == selected.index:
                    continue
                hostapi = device.hostapi_name.lower()
                if self.meeting_compatibility and ("wdm-ks" in hostapi or "wdm" in hostapi):
                    # WDM-KS is a late/exclusive-prone backend. Do not use it
                    # automatically in meeting compatibility mode.
                    continue
                add(device)
            log.info("Audio candidates restricted to selected microphone: %s", len(candidates))
            return candidates

        # No explicit microphone selected: use only the Windows/default input
        # endpoints. Autodetect remains available when the user wants the app to
        # search across all microphones.
        if self.meeting_compatibility:
            add(_default_wasapi_input_candidate(devices), "Системный микрофон по умолчанию · Windows WASAPI")
        add(None)
        log.info("Audio candidates restricted to default microphone: %s", len(candidates))
        return candidates

    def _extra_settings_for(self, candidate: AudioOpenCandidate):  # noqa: ANN001
        if not candidate.use_wasapi_shared:
            return None
        try:
            return sd.WasapiSettings(exclusive=False)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not create WASAPI shared settings: %s", exc)
            return None

    def _log_devices(self) -> None:
        try:
            lines = []
            for device in list_input_devices():
                lines.append(
                    f"index={device.index} id={device.device_id!r} label={device.label!r} hostapi={device.hostapi_name!r} "
                    f"channels={device.max_input_channels} default_samplerate={device.default_samplerate}"
                )
            log.info("Available input devices:\n%s", "\n".join(lines) if lines else "No input devices")
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not log input devices: %s", exc)

    def start(self) -> None:
        if self._stream is not None:
            return
        self._frames = []
        self._started_at = time.perf_counter()

        def callback(indata, frames, time_info, status) -> None:  # noqa: ANN001
            if status:
                log.warning("PortAudio input status: %s", status)
            self._frames.append(indata.copy())

        errors: list[str] = []
        candidates = self._candidate_devices()
        requested_label = "Системный по умолчанию" if self.input_device is None else self.input_device.label
        log.info(
            "Selected input device: id=%r label=%s meeting_compatibility=%s candidate_count=%s",
            self.input_device_id,
            requested_label,
            self.meeting_compatibility,
            len(candidates),
        )

        for candidate in candidates:
            extra_settings = self._extra_settings_for(candidate)
            for rate in self._rate_candidates(candidate.preferred_rates):
                try:
                    log.info(
                        "Open input stream attempt: device=%s label=%r hostapi=%s sample_rate=%s channels=%s wasapi_shared=%s",
                        candidate.device_index,
                        candidate.label,
                        candidate.hostapi_name,
                        rate,
                        self.channels,
                        candidate.use_wasapi_shared,
                    )
                    kwargs = {
                        "samplerate": rate,
                        "device": candidate.device_index,
                        "channels": self.channels,
                        "dtype": "float32",
                        "callback": callback,
                    }
                    if extra_settings is not None:
                        kwargs["extra_settings"] = extra_settings
                    stream = sd.InputStream(**kwargs)
                    stream.start()
                    self._stream = stream
                    self.sample_rate = rate
                    self._active_label = candidate.label
                    log.info(
                        "Input stream opened: device=%s label=%r sample_rate=%s wasapi_shared=%s",
                        candidate.device_index,
                        candidate.label,
                        rate,
                        candidate.use_wasapi_shared,
                    )
                    return
                except Exception as exc:  # noqa: BLE001
                    error = f"{candidate.label} · {rate} Hz: {exc}"
                    errors.append(error)
                    log.warning("Input stream open failed: %s", error)

        self._started_at = None
        self._log_devices()
        meeting_hint = (
            "\n\nПохоже, микрофон занят другим приложением или драйвер не разрешает совместную запись. "
            "Это часто происходит во время онлайн-созвона, если Zoom/Teams/браузер или драйвер удерживает устройство."
        )
        raise RuntimeError(
            "Запись недоступна. Не удалось открыть выбранный микрофон."
            + meeting_hint
            + "\n\nЧто можно сделать:\n"
            "1. Выберите другой микрофон в настройках Voice Input Local.\n"
            "2. Проверьте доступ к микрофону для классических приложений Windows.\n"
            "3. В свойствах микрофона Windows отключите эксклюзивный режим, если он включён.\n"
            "4. Если идёт созвон или открыт OBS, убедитесь, что выбран нужный микрофон и он не открыт в эксклюзивном режиме.\n"
            "5. Для поиска другого доступного микрофона используйте «Автонастройка микрофона».\n\n"
            f"Выбранное устройство: {requested_label}\n\n"
            "Попытки открытия:\n- " + "\n- ".join(errors[-18:]) + "\n\nСписок аудиоустройств записан в app.log."
        )

    def cancel(self) -> None:
        """Stop recording and discard captured audio."""
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()
        self._frames = []
        self._started_at = None

    def snapshot_audio(self) -> tuple[np.ndarray, float]:
        """Return a copy of audio captured so far and its duration in seconds."""
        if not self._frames:
            return np.array([], dtype=np.float32), 0.0
        audio = np.concatenate([frame.copy() for frame in self._frames], axis=0).reshape(-1)
        audio = np.nan_to_num(audio).astype(np.float32)
        audio = np.clip(audio, -1.0, 1.0)
        return audio, float(len(audio)) / float(self.sample_rate)

    def snapshot_segment(self, start_seconds: float, end_seconds: float | None = None) -> tuple[np.ndarray, float]:
        """Return an audio slice from the current recording."""
        audio, duration = self.snapshot_audio()
        if audio.size == 0:
            return audio, 0.0
        end = duration if end_seconds is None else max(0.0, min(duration, end_seconds))
        start = max(0.0, min(start_seconds, end))
        start_idx = int(start * self.sample_rate)
        end_idx = int(end * self.sample_rate)
        sliced = audio[start_idx:end_idx]
        return sliced, float(len(sliced)) / float(self.sample_rate)

    def write_audio_to_wav(self, audio: np.ndarray, prefix: str = "dictation") -> Path:
        if audio.size == 0:
            raise RuntimeError("Нет записанного звука. Проверьте доступ к микрофону и выбранное устройство ввода.")
        pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16)
        path = recordings_dir() / f"{prefix}-{uuid.uuid4().hex}.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(self.channels)
            wav.setsampwidth(2)
            wav.setframerate(self.sample_rate)
            wav.writeframes(pcm.tobytes())
        return path

    def snapshot_to_wav(self, prefix: str = "live") -> tuple[Path, float]:
        audio, duration = self.snapshot_audio()
        return self.write_audio_to_wav(audio, prefix=prefix), duration

    def snapshot_segment_to_wav(self, start_seconds: float, end_seconds: float | None = None, prefix: str = "live") -> tuple[Path, float]:
        audio, duration = self.snapshot_segment(start_seconds, end_seconds)
        return self.write_audio_to_wav(audio, prefix=prefix), duration

    def stop_to_wav(self) -> tuple[Path, float]:
        if self._stream is None:
            raise RuntimeError("Запись не активна")
        stream = self._stream
        self._stream = None
        stream.stop()
        stream.close()
        audio, duration = self.snapshot_audio()
        self._started_at = None
        if audio.size == 0:
            raise RuntimeError("Нет записанного звука. Проверьте доступ к микрофону и выбранное устройство ввода.")
        path = self.write_audio_to_wav(audio, prefix="dictation")
        return path, duration


def auto_detect_input_device(test_seconds: float = 0.65, meeting_compatibility: bool = True) -> tuple[str, str, int, float]:
    """Find an openable input device and return (device_id, label, sample_rate, rms_level).

    This is used on first launch and by the Settings button. It does not save audio.
    The function prefers WASAPI/shared-compatible devices but will fall back to other
    backends when they are the only ones that open on a given Windows machine.
    """
    devices = list_input_devices()
    if not devices:
        raise RuntimeError("Микрофоны не найдены. Проверьте подключение устройства и разрешения Windows.")

    # Prefer the same order as regular recording: WASAPI, DirectSound, WDM-KS, MME.
    ordered = sorted(devices, key=lambda d: (_api_rank(d.hostapi_name), d.name.lower(), d.index))
    errors: list[str] = []

    for device in ordered:
        preferred_rates: list[int] = []
        if device.default_samplerate > 0:
            preferred_rates.append(device.default_samplerate)
        preferred_rates.extend([48000, 44100, 32000, 16000])
        # Reuse AudioRecorder's de-dup/rate ordering.
        dummy = AudioRecorder(input_device_id=device.device_id, meeting_compatibility=meeting_compatibility)
        rates = dummy._rate_candidates(preferred_rates)
        use_wasapi = "wasapi" in device.hostapi_name.lower()
        extra_settings = None
        if use_wasapi:
            try:
                extra_settings = sd.WasapiSettings(exclusive=False)
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not create WASAPI shared settings for autodetect: %s", exc)

        for rate in rates:
            frames: list[np.ndarray] = []

            def callback(indata, frame_count, time_info, status) -> None:  # noqa: ANN001
                if status:
                    log.warning("Autodetect input status for %s: %s", device.label, status)
                frames.append(indata.copy())

            try:
                log.info("Microphone autodetect attempt: device=%s label=%r rate=%s wasapi_shared=%s", device.index, device.label, rate, bool(extra_settings))
                kwargs = {
                    "samplerate": rate,
                    "device": device.index,
                    "channels": 1,
                    "dtype": "float32",
                    "callback": callback,
                }
                if extra_settings is not None:
                    kwargs["extra_settings"] = extra_settings
                stream = sd.InputStream(**kwargs)
                with stream:
                    sd.sleep(max(250, int(test_seconds * 1000)))
                if frames:
                    audio = np.concatenate(frames, axis=0).reshape(-1)
                    audio = np.nan_to_num(audio).astype(np.float32)
                    rms = float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0
                else:
                    rms = 0.0
                log.info("Microphone autodetect success: id=%s label=%r rate=%s rms=%.6f", device.device_id, device.label, rate, rms)
                return device.device_id, device.label, rate, rms
            except Exception as exc:  # noqa: BLE001
                error = f"{device.label} · {rate} Hz: {exc}"
                errors.append(error)
                log.warning("Microphone autodetect failed: %s", error)

    raise RuntimeError(
        "Не удалось найти доступный микрофон. Проверьте подключение устройства, разрешения Windows и занятость микрофона другими приложениями.\n\n"
        "Попытки:\n- " + "\n- ".join(errors[-20:])
    )
