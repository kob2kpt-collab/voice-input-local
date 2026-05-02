from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .paths import config_path


@dataclass
class AppConfig:
    hotkey: str = "ctrl+alt+space"
    selected_model: str = "whisper:small"
    auto_paste: bool = True
    paste_only_when_text_field_detected: bool = True
    language: str = ""  # empty means auto for Whisper and Parakeet v3
    device: str = "cpu"  # cpu, cuda, auto
    compute_type: str = "int8"  # int8, int8_float16, float16, float32
    sample_rate: int = 16000
    audio_input_device_id: str = ""  # empty means system default input device
    audio_meeting_compatibility: bool = True  # prefer WASAPI shared/fallbacks during online meetings
    save_audio_debug: bool = False
    overlay_enabled: bool = True
    overlay_x: int | None = None
    overlay_y: int | None = None
    autostart_enabled: bool = False
    hf_token: str = ""  # optional Hugging Face token for authenticated model downloads
    updates_enabled: bool = True
    update_repo: str = ""  # owner/repo for GitHub Releases, e.g. my-org/voice-input-local
    last_update_check_ts: float = 0.0
    microphone_autodetect_done: bool = False
    file_stable_timestamps_enabled: bool = False
    file_diarization_enabled: bool = False
    file_speaker_count: str = "auto"  # auto, 2, 3, 4
    live_transcription: bool = False
    live_insert_confirmed_text: bool = False
    live_update_interval_seconds: float = 2.0

    @classmethod
    def load(cls, path: Path | None = None) -> "AppConfig":
        path = path or config_path()
        if not path.exists():
            cfg = cls()
            cfg.save(path)
            return cfg
        try:
            data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            base = asdict(cls())
            base.update({k: v for k, v in data.items() if k in base})
            cfg = cls(**base)
            # v3.4 migration: live mode is disabled in the stable build because
            # the previous near-live implementation produced high latency and
            # empty partial results on short utterances.
            cfg.live_transcription = False
            cfg.live_insert_confirmed_text = False
            return cfg
        except Exception:
            return cls()

    def save(self, path: Path | None = None) -> None:
        path = path or config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")
