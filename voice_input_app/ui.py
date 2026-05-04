from __future__ import annotations

import os
import json
import subprocess
import sys
import time
from pathlib import Path
from string import punctuation

import pyperclip
from PySide6.QtCore import QObject, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QCloseEvent, QIcon, QKeySequence, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QKeySequenceEdit,
    QSystemTrayIcon,
    QStyle,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import autostart
from .audio_recorder import AudioRecorder, list_input_devices
from .audio_files import SUPPORTED_AUDIO_EXTENSIONS, format_duration, is_supported_audio_file
from . import __version__
from .config import AppConfig
from .history import HistoryItem, HistoryStore
from .hotkeys import HotkeyService, normalize_hotkey
from .insert import copy_and_maybe_paste, focused_control_accepts_text, foreground_belongs_to_current_process, foreground_matches_window_handle
from .logger import get_logger, setup_logging
from .models import ALL_MODELS, DEFAULT_MODEL_KEY, DownloadProgress, ModelManager, merge_transcript_parts, model_display_name
from .overlay import RecordingOverlay
from .paths import app_icon_path, logs_dir
from .updater import UpdateInfo, launch_update_file, normalize_repo
from .workers import DownloadWorker, FileProgress, FileTranscribeWorker, FileTranscriptBlock, MicrophoneAutodetectWorker, MicrophoneAutodetectResult, PreloadWorker, TranscribeWorker, UpdateCheckWorker, UpdateDownloadWorker

log = get_logger("ui")

APP_STYLE = """
QMainWindow { background: #101114; color: #f4f4f5; }
QWidget { font-size: 13px; color: #f4f4f5; }
QLabel#Title { font-size: 24px; font-weight: 700; }
QLabel#Subtitle { color: #a1a1aa; }
QLabel#RecordBadge { border-radius: 14px; padding: 8px 12px; background: #27272a; }
QPushButton { background: #27272a; border: 1px solid #3f3f46; border-radius: 10px; padding: 9px 12px; }
QPushButton:hover { background: #34343b; }
QPushButton:pressed { background: #52525b; border-color: #71717a; padding-top: 10px; padding-bottom: 8px; }
QPushButton:disabled { background: #1f1f23; color: #71717a; border-color: #2b2b31; }
QPushButton#Primary { background: #f4f4f5; color: #111113; font-weight: 700; }
QPushButton#Primary:hover { background: #ffffff; }
QPushButton#Primary:pressed { background: #d4d4d8; color: #111113; }
QPushButton#Danger { background: #451a1a; border-color: #7f1d1d; }
QPushButton#Danger:hover { background: #5f2020; }
QPushButton#Danger:pressed { background: #7f1d1d; }
QTextEdit, QListWidget, QTableWidget, QComboBox, QLineEdit, QKeySequenceEdit {
    background: #18181b; border: 1px solid #3f3f46; border-radius: 10px; padding: 6px;
}
QKeySequenceEdit:focus { border: 2px solid #fbbf24; background: #1f2937; }
QKeySequenceEdit[needsAttention="true"] { border: 2px solid #f97316; background: #2a160f; }
QTableWidget { gridline-color: #27272a; selection-background-color: #334155; selection-color: #ffffff; }
QTableWidget::item { padding: 6px; }
QTableWidget::item:selected { background: #334155; color: #ffffff; }
QListWidget::item { padding: 8px; border-radius: 8px; margin: 2px; }
QListWidget::item:selected { background: #334155; color: #ffffff; }
QCheckBox { spacing: 9px; }
QCheckBox::indicator { width: 17px; height: 17px; border-radius: 4px; border: 1px solid #f8fafc; background: #111827; }
QCheckBox::indicator:hover { border: 1px solid #ffffff; background: #1f2937; }
QCheckBox::indicator:checked { background: #f8fafc; border: 1px solid #f8fafc; image: none; }
QCheckBox::indicator:disabled { border: 1px solid #64748b; background: #27272a; }
QTabWidget::pane { border: 1px solid #27272a; border-radius: 14px; }
QTabBar::tab { background: #18181b; color: #d4d4d8; padding: 8px 12px; border-radius: 9px; margin: 4px; }
QTabBar::tab:selected { background: #f4f4f5; color: #111113; }
"""


class HotkeySignal(QObject):
    triggered = Signal()


class CancelSignal(QObject):
    triggered = Signal()


def _format_history_date(value: str) -> str:
    from datetime import datetime

    try:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return value.replace("T", " ")[:16]


def _normal_word(word: str) -> str:
    return word.strip().strip(punctuation + "«»“”„…—–").lower()


def _word_delta(previous: str, current: str) -> str:
    prev_words = previous.split()
    cur_words = current.split()
    common = 0
    for a, b in zip(prev_words, cur_words):
        if _normal_word(a) != _normal_word(b):
            break
        common += 1
    return " ".join(cur_words[common:]).strip()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Voice Input Local")
        self.resize(980, 720)
        self.setStyleSheet(APP_STYLE)
        self.app_icon = QIcon(str(app_icon_path()))
        if not self.app_icon.isNull():
            self.setWindowIcon(self.app_icon)

        self.cfg = AppConfig.load()
        self.history = HistoryStore()
        self.models = ModelManager()
        self.recorder = AudioRecorder(sample_rate=self.cfg.sample_rate, input_device_id=self.cfg.audio_input_device_id, meeting_compatibility=self.cfg.audio_meeting_compatibility)
        self.transcribe_worker: TranscribeWorker | None = None
        self.file_transcribe_worker: FileTranscribeWorker | None = None
        self.microphone_autodetect_worker: MicrophoneAutodetectWorker | None = None
        self.update_check_worker: UpdateCheckWorker | None = None
        self.update_download_worker: UpdateDownloadWorker | None = None
        self.pending_update_info: UpdateInfo | None = None
        self.file_cancel_requested = False
        self.selected_file_path: Path | None = None
        self._file_transcript_blocks: list[dict[str, object]] = []
        self._file_show_timestamps_for_job = False
        self.live_worker: TranscribeWorker | None = None
        self.download_worker: DownloadWorker | None = None
        self.preload_worker: PreloadWorker | None = None
        self.pending_final: tuple[Path, float, AppConfig] | None = None
        self.hotkey_signal = HotkeySignal()
        self.hotkey_signal.triggered.connect(self.toggle_recording)
        self.cancel_signal = CancelSignal()
        self.cancel_signal.triggered.connect(self.cancel_current_action)
        self.hotkey = HotkeyService(lambda: self.hotkey_signal.triggered.emit())
        self.cancel_hotkey_handle = None
        self.record_blink = False
        self.overlay = RecordingOverlay()
        self.overlay.restore_position(self.cfg.overlay_x, self.cfg.overlay_y)
        self.overlay.copy_requested.connect(self.copy_overlay_result)
        self.overlay.position_changed.connect(self.on_overlay_position_changed)
        self.model_status_overrides: dict[str, str] = {}
        self.downloading_keys: set[str] = set()
        self.download_progress_frames = ["◌", "◔", "◑", "◕"]
        self.download_progress_tick = 0
        self.result_preview_active = False
        self.result_preview_text = ""
        self.microphone_test_recorder: AudioRecorder | None = None
        self.recording_started_in_own_window = False
        self._settings_loading = True
        self._settings_save_timer = QTimer(self)
        self._settings_save_timer.setSingleShot(True)
        self._settings_save_timer.timeout.connect(self.auto_save_settings)

        self.cancel_requested = False
        self.live_last_request_at = 0.0
        self.live_processed_until_seconds = 0.0
        self.live_total_text = ""
        self.live_had_insertions = False
        self.live_target_is_text_field = False
        self.live_unavailable_notice_shown = False

        self.ensure_selected_model_available()
        self._build_ui()
        self._build_tray()
        self._load_settings_into_ui()
        self._connect_settings_autosave()
        self._settings_loading = False
        self.refresh_models_table()
        self.refresh_available_models_combo()
        self.refresh_history()
        self.register_hotkey(show_errors=False)
        self._sync_overlay_visibility()
        self.start_preload_selected_model()
        QTimer.singleShot(900, self.maybe_start_first_microphone_autodetect)
        QTimer.singleShot(1800, lambda: self.check_for_updates(manual=False))

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.on_timer_tick)
        self.timer.start(300)

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(24, 22, 24, 24)
        root.setSpacing(14)

        title = QLabel("Voice Input Local")
        title.setObjectName("Title")
        subtitle = QLabel("Локальный голосовой ввод для Windows: горячая клавиша, индикатор записи, модели, буфер обмена и история.")
        subtitle.setObjectName("Subtitle")
        root.addWidget(title)
        root.addWidget(subtitle)

        status_row = QHBoxLayout()
        self.record_badge = QLabel("Ожидание")
        self.record_badge.setObjectName("RecordBadge")
        self.status_label = QLabel("Готово")
        self.status_label.setObjectName("Subtitle")
        status_row.addWidget(self.record_badge)
        status_row.addWidget(self.status_label, 1)
        root.addLayout(status_row)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._dictation_tab(), "Диктовка")
        self.tabs.addTab(self._files_tab(), "Файлы")
        self.tabs.addTab(self._models_tab(), "Модели")
        self.tabs.addTab(self._settings_tab(), "Настройки")
        self.tabs.addTab(self._history_tab(), "История")
        root.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

    def _dictation_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        row = QHBoxLayout()
        self.model_combo = QComboBox()
        self.model_combo.setToolTip("Выберите загруженную модель. Она станет активной сразу и начнёт готовиться в фоне.")
        self.model_combo.currentIndexChanged.connect(self.on_dictation_model_changed)
        row.addWidget(QLabel("Активная модель:"))
        row.addWidget(self.model_combo, 1)
        layout.addLayout(row)

        self.toggle_btn = QPushButton("Начать запись")
        self.toggle_btn.setObjectName("Primary")
        self.toggle_btn.clicked.connect(self.toggle_recording)
        layout.addWidget(self.toggle_btn)

        self.last_text = QTextEdit()
        self.last_text.setPlaceholderText("Последняя расшифровка появится здесь.")
        layout.addWidget(self.last_text, 1)

        actions = QHBoxLayout()
        copy_btn = QPushButton("Скопировать текст")
        copy_btn.clicked.connect(lambda: pyperclip.copy(self.last_text.toPlainText()))
        insert_btn = QPushButton("Вставить текст сейчас")
        insert_btn.clicked.connect(lambda: copy_and_maybe_paste(self.last_text.toPlainText(), True, self.cfg.paste_only_when_text_field_detected))
        actions.addWidget(copy_btn)
        actions.addWidget(insert_btn)
        actions.addStretch(1)
        layout.addLayout(actions)
        return tab

    def _files_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        info = QLabel(
            "Расшифровка файла работает отдельно от диктовки: текст не вставляется автоматически "
            "и не копируется в буфер. После завершения пользователь сам копирует результат."
        )
        info.setWordWrap(True)
        info.setObjectName("Subtitle")
        layout.addWidget(info)

        file_row = QHBoxLayout()
        self.file_path_edit = QLineEdit()
        self.file_path_edit.setReadOnly(True)
        self.file_path_edit.setPlaceholderText("Выберите аудио или видео файл: wav, mp3, m4a, mp4, webm, ogg, flac…")
        self.choose_file_btn = QPushButton("Выбрать файл")
        self.choose_file_btn.clicked.connect(self.choose_audio_file)
        file_row.addWidget(self.file_path_edit, 1)
        file_row.addWidget(self.choose_file_btn)
        layout.addLayout(file_row)

        model_row = QHBoxLayout()
        self.file_model_combo = QComboBox()
        self.file_model_combo.setToolTip("Для файлов доступны те же загруженные модели, что и для диктовки.")
        model_row.addWidget(QLabel("Модель:"))
        model_row.addWidget(self.file_model_combo, 1)
        layout.addLayout(model_row)

        options_box = QVBoxLayout()
        self.file_stable_timestamps_check = QCheckBox("Точные таймкоды (доп. VAD-модель, медленнее)")
        self.file_stable_timestamps_check.setToolTip("Выключено по умолчанию. Улучшает разбивку фраз и таймкоды при обработке файлов.")
        self.file_diarization_check = QCheckBox("Определять говорящих (диаризация, до 4 спикеров)")
        self.file_diarization_check.setToolTip("Выключено по умолчанию. Требует дополнительную Sortformer ONNX-модель. Доступно только для обработки файлов.")
        speaker_row = QHBoxLayout()
        self.file_speaker_count_combo = QComboBox()
        self.file_speaker_count_combo.addItem("Авто", "auto")
        self.file_speaker_count_combo.addItem("2", "2")
        self.file_speaker_count_combo.addItem("3", "3")
        self.file_speaker_count_combo.addItem("4", "4")
        speaker_row.addWidget(QLabel("Количество говорящих:"))
        speaker_row.addWidget(self.file_speaker_count_combo)
        speaker_row.addStretch(1)
        options_box.addWidget(self.file_stable_timestamps_check)
        options_box.addWidget(self.file_diarization_check)
        options_box.addLayout(speaker_row)
        layout.addLayout(options_box)

        action_row = QHBoxLayout()
        self.file_transcribe_btn = QPushButton("Расшифровать файл")
        self.file_transcribe_btn.setObjectName("Primary")
        self.file_transcribe_btn.clicked.connect(self.start_file_transcription)
        self.file_cancel_btn = QPushButton("Отменить")
        self.file_cancel_btn.setObjectName("Danger")
        self.file_cancel_btn.setEnabled(False)
        self.file_cancel_btn.clicked.connect(self.cancel_file_transcription)
        self.file_copy_btn = QPushButton("Скопировать результат")
        self.file_copy_btn.clicked.connect(lambda: pyperclip.copy(self.file_result_text.toPlainText()))
        self.file_copy_btn.setEnabled(False)
        action_row.addWidget(self.file_transcribe_btn)
        action_row.addWidget(self.file_cancel_btn)
        action_row.addWidget(self.file_copy_btn)
        action_row.addStretch(1)
        layout.addLayout(action_row)

        self.file_progress = QProgressBar()
        self.file_progress.setVisible(False)
        self.file_progress.setTextVisible(True)
        layout.addWidget(self.file_progress)

        self.file_status_label = QLabel("Файл не выбран")
        self.file_status_label.setObjectName("Subtitle")
        self.file_status_label.setWordWrap(True)
        layout.addWidget(self.file_status_label)

        self.file_result_text = QTextEdit()
        self.file_result_text.setReadOnly(True)
        self.file_result_text.setPlaceholderText("Здесь появится результат расшифровки файла.")
        layout.addWidget(self.file_result_text, 1)
        return tab

    def _models_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(18, 18, 18, 18)
        self.models_table = QTableWidget(0, 6)
        self.models_table.setHorizontalHeaderLabels(["Движок", "Модель", "Язык", "Размер", "Статус", "Комментарий"])
        self.models_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.models_table.setSelectionMode(QTableWidget.SingleSelection)
        self.models_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.models_table.verticalHeader().setVisible(False)
        self.models_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.models_table, 1)

        row = QHBoxLayout()
        self.download_btn = QPushButton("Загрузить выбранную")
        self.delete_btn = QPushButton("Удалить выбранную")
        self.delete_btn.setObjectName("Danger")
        self.use_model_btn_2 = QPushButton("Использовать выбранную")
        self.download_btn.clicked.connect(self.download_selected_model)
        self.delete_btn.clicked.connect(self.delete_selected_model)
        self.use_model_btn_2.clicked.connect(self.use_selected_model_from_table)
        row.addWidget(self.download_btn)
        row.addWidget(self.delete_btn)
        row.addWidget(self.use_model_btn_2)
        row.addStretch(1)
        layout.addLayout(row)
        return tab

    def _settings_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)
        form = QFormLayout()

        self.hotkey_edit = QKeySequenceEdit()
        self.auto_paste_check = QCheckBox("Автоматически вставлять текст в активное поле после расшифровки")
        self.detect_text_field_check = QCheckBox("Вставлять только если Windows видит активное поле ввода")
        self.detect_text_field_check.setToolTip("Если выключить, приложение будет пробовать Ctrl+V в активное окно без строгой проверки поля ввода.")
        self.meeting_compat_check = QCheckBox("Режим совместимости с онлайн-созвонами")
        self.meeting_compat_check.setToolTip("Рекомендуется: приоритет Windows WASAPI shared и fallback на другие устройства, если микрофон занят Zoom/Teams/браузером.")
        self.overlay_enabled_check = QCheckBox("Показывать плавающую плашку поверх всех окон")
        self.autostart_check = QCheckBox("Запускать Voice Input Local вместе с Windows")
        self.hf_token_edit = QLineEdit()
        self.hf_token_edit.setEchoMode(QLineEdit.Password)
        self.hf_token_edit.setPlaceholderText("hf_… (необязательно)")
        self.hf_token_edit.setToolTip("Необязательный Hugging Face token для авторизованной загрузки моделей. Также можно использовать переменные HF_TOKEN или HUGGINGFACE_HUB_TOKEN.")
        self.updates_enabled_check = QCheckBox("Проверять обновления автоматически")
        self.update_repo_edit = QLineEdit()
        self.update_repo_edit.setPlaceholderText("owner/repo, например your-org/voice-input-local")
        self.update_repo_edit.setToolTip("Публичный GitHub-репозиторий с релизами приложения. Используется для централизованных обновлений.")
        self.microphone_combo = QComboBox()
        self.refresh_microphones_btn = QPushButton("Обновить список")
        self.refresh_microphones_btn.clicked.connect(lambda: self.refresh_microphone_combo())
        if not autostart.is_supported():
            self.autostart_check.setEnabled(False)
            self.autostart_check.setToolTip("Автозагрузка доступна только в Windows.")

        self.language_combo = QComboBox()
        self.language_combo.addItem("Авто", "")
        for code in ["ru", "en", "kk", "de", "fr", "es", "it", "nl", "pt", "uk", "pl"]:
            self.language_combo.addItem(code, code)
        self.device_combo = QComboBox()
        for v in ["cpu", "cuda", "auto"]:
            self.device_combo.addItem(v, v)
        self.compute_combo = QComboBox()
        for v in ["int8", "int8_float16", "float16", "float32"]:
            self.compute_combo.addItem(v, v)

        form.addRow("Горячая клавиша", self.hotkey_edit)
        self.hotkey_hint_label = QLabel("Кликните поле и нажмите новую комбинацию. Если комбинация недоступна, поле подсветится.")
        self.hotkey_hint_label.setObjectName("Subtitle")
        self.hotkey_hint_label.setWordWrap(True)
        form.addRow("", self.hotkey_hint_label)
        form.addRow("Вставка", self.auto_paste_check)
        form.addRow("Безопасная вставка", self.detect_text_field_check)
        form.addRow("Созвоны", self.meeting_compat_check)
        form.addRow("Плавающая плашка", self.overlay_enabled_check)
        form.addRow("Автозагрузка", self.autostart_check)
        form.addRow("Hugging Face token", self.hf_token_edit)
        form.addRow("Обновления", self.updates_enabled_check)
        form.addRow("GitHub repo", self.update_repo_edit)
        microphone_row = QWidget()
        microphone_layout = QHBoxLayout(microphone_row)
        microphone_layout.setContentsMargins(0, 0, 0, 0)
        microphone_layout.addWidget(self.microphone_combo, 1)
        microphone_layout.addWidget(self.refresh_microphones_btn)
        form.addRow("Микрофон", microphone_row)
        form.addRow("Язык Whisper", self.language_combo)
        form.addRow("Ускорение Whisper", self.device_combo)
        form.addRow("Compute Whisper", self.compute_combo)
        layout.addLayout(form)

        hint = QLabel("Настройки сохраняются автоматически. Кнопку сохранения нажимать больше не нужно.")
        hint.setWordWrap(True)
        hint.setObjectName("Subtitle")
        layout.addWidget(hint)

        mic_hint = QLabel(
            "Если приложение не видно в списке Windows, проверьте переключатель «Разрешить классическим приложениям доступ к микрофону». "
            "Режим совместимости с созвонами использует WASAPI shared-first и fallback на другие input-устройства, чтобы снизить конфликты с Zoom/Teams/Meet. "
            "При запуске через bat Windows может показывать python.exe, а после сборки exe — VoiceInputLocal.exe."
        )
        mic_hint.setWordWrap(True)
        mic_hint.setObjectName("Subtitle")
        layout.addWidget(mic_hint)

        buttons = QHBoxLayout()
        self.test_mic_btn = QPushButton("Проверить микрофон")
        self.test_mic_btn.setProperty("originalText", "Проверить микрофон")
        self.test_mic_btn.clicked.connect(self.test_microphone_access)
        self.autodetect_mic_btn = QPushButton("Автонастройка микрофона")
        self.autodetect_mic_btn.setProperty("originalText", "Автонастройка микрофона")
        self.autodetect_mic_btn.clicked.connect(lambda: self.start_microphone_autodetect(manual=True))
        privacy_btn = QPushButton("Настройки микрофона Windows")
        privacy_btn.clicked.connect(self.open_microphone_privacy_settings)
        sound_btn = QPushButton("Настройки звука Windows")
        sound_btn.clicked.connect(self.open_sound_settings)
        logs_btn = QPushButton("Открыть папку логов")
        logs_btn.clicked.connect(self.open_logs_folder)
        self.check_updates_btn = QPushButton("Проверить обновления")
        self.check_updates_btn.setProperty("originalText", "Проверить обновления")
        self.check_updates_btn.clicked.connect(lambda: self.check_for_updates(manual=True))
        buttons.addWidget(self.test_mic_btn)
        buttons.addWidget(self.autodetect_mic_btn)
        buttons.addWidget(privacy_btn)
        buttons.addWidget(sound_btn)
        buttons.addWidget(logs_btn)
        buttons.addWidget(self.check_updates_btn)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        layout.addStretch(1)
        return tab

    def _history_tab(self) -> QWidget:
        tab = QWidget()
        layout = QHBoxLayout(tab)
        layout.setContentsMargins(18, 18, 18, 18)
        self.history_list = QListWidget()
        self.history_list.currentItemChanged.connect(self.show_history_item)
        layout.addWidget(self.history_list, 1)

        right = QVBoxLayout()
        self.history_text = QTextEdit()
        self.history_text.setReadOnly(True)
        self.history_text.setPlaceholderText("Выберите запись слева, чтобы посмотреть текст.")
        right.addWidget(self.history_text, 1)
        copy_btn = QPushButton("Скопировать выбранную")
        copy_btn.clicked.connect(lambda: pyperclip.copy(self.history_text.toPlainText()))
        delete_btn = QPushButton("Удалить выбранную")
        delete_btn.setObjectName("Danger")
        delete_btn.clicked.connect(self.delete_history_item)
        clear_btn = QPushButton("Очистить историю")
        clear_btn.setObjectName("Danger")
        clear_btn.clicked.connect(self.clear_history)
        right.addWidget(copy_btn)
        right.addWidget(delete_btn)
        right.addWidget(clear_btn)
        layout.addLayout(right, 2)
        return tab

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(self.app_icon if not self.app_icon.isNull() else self.style().standardIcon(QStyle.SP_ComputerIcon))
        menu = self.tray.contextMenu()
        if menu is None:
            from PySide6.QtWidgets import QMenu

            menu = QMenu()
            self.tray.setContextMenu(menu)
        show_action = QAction("Показать", self)
        show_action.triggered.connect(self.show_from_tray)
        toggle_action = QAction("Начать/остановить запись", self)
        toggle_action.triggered.connect(self.toggle_recording)
        cancel_action = QAction("Отменить запись/распознавание", self)
        cancel_action.triggered.connect(self.cancel_current_action)
        quit_action = QAction("Выйти", self)
        quit_action.triggered.connect(self.really_quit)
        menu.addAction(show_action)
        menu.addAction(toggle_action)
        menu.addAction(cancel_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.tray.setToolTip("Voice Input Local")
        self.tray.activated.connect(self.on_tray_activated)
        self.tray.show()

    def show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def on_tray_activated(self, reason) -> None:  # noqa: ANN001
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_from_tray()


    def refresh_microphone_combo(self) -> None:
        current = self.cfg.audio_input_device_id
        if hasattr(self, "microphone_combo"):
            selected = self.microphone_combo.currentData()
            if selected is not None and not self._settings_loading:
                current = str(selected)
        self.microphone_combo.blockSignals(True)
        try:
            self.microphone_combo.clear()
            self.microphone_combo.addItem("Системный микрофон по умолчанию", "")
            try:
                devices = list_input_devices()
                for device in devices:
                    self.microphone_combo.addItem(device.label, device.device_id)
                if not devices:
                    self.status_label.setText("Микрофоны не найдены. Проверьте подключение и разрешения Windows.")
            except Exception as exc:  # noqa: BLE001
                log.exception("Could not refresh microphone list")
                self.status_label.setText(f"Не удалось получить список микрофонов: {exc}")
            self._set_combo_value(self.microphone_combo, current)
        finally:
            self.microphone_combo.blockSignals(False)

    def _load_settings_into_ui(self) -> None:
        self.hotkey_edit.setKeySequence(QKeySequence(self.cfg.hotkey))
        self.auto_paste_check.setChecked(self.cfg.auto_paste)
        self.detect_text_field_check.setChecked(self.cfg.paste_only_when_text_field_detected)
        self.meeting_compat_check.setChecked(self.cfg.audio_meeting_compatibility)
        self.overlay_enabled_check.setChecked(self.cfg.overlay_enabled)
        autostart_enabled = autostart.is_enabled() if autostart.is_supported() else False
        self.cfg.autostart_enabled = autostart_enabled
        self.autostart_check.setChecked(autostart_enabled)
        self.hf_token_edit.setText(self.cfg.hf_token)
        self.updates_enabled_check.setChecked(self.cfg.updates_enabled)
        self.update_repo_edit.setText(self.cfg.update_repo)
        self.refresh_microphone_combo()
        self._set_combo_value(self.microphone_combo, self.cfg.audio_input_device_id)
        self._set_combo_value(self.language_combo, self.cfg.language)
        self._set_combo_value(self.device_combo, self.cfg.device)
        self._set_combo_value(self.compute_combo, self.cfg.compute_type)
        self._set_combo_value(self.model_combo, self.cfg.selected_model)
        if hasattr(self, "file_stable_timestamps_check"):
            self.file_stable_timestamps_check.setChecked(self.cfg.file_stable_timestamps_enabled)
        if hasattr(self, "file_diarization_check"):
            self.file_diarization_check.setChecked(self.cfg.file_diarization_enabled)
        if hasattr(self, "file_speaker_count_combo"):
            self._set_combo_value(self.file_speaker_count_combo, self.cfg.file_speaker_count)

    def _connect_settings_autosave(self) -> None:
        self.hotkey_edit.editingFinished.connect(self.schedule_settings_autosave)
        self.hf_token_edit.editingFinished.connect(self.schedule_settings_autosave)
        self.update_repo_edit.editingFinished.connect(self.schedule_settings_autosave)
        for checkbox in [self.auto_paste_check, self.detect_text_field_check, self.meeting_compat_check, self.overlay_enabled_check, self.autostart_check, self.updates_enabled_check]:
            checkbox.stateChanged.connect(self.schedule_settings_autosave)
        for combo in [self.microphone_combo, self.language_combo, self.device_combo, self.compute_combo]:
            combo.currentIndexChanged.connect(self.schedule_settings_autosave)
        self.file_stable_timestamps_check.stateChanged.connect(self.save_file_options)
        self.file_diarization_check.stateChanged.connect(self.save_file_options)
        self.file_speaker_count_combo.currentIndexChanged.connect(self.save_file_options)

    def save_file_options(self) -> None:
        if self._settings_loading:
            return
        self.cfg.file_stable_timestamps_enabled = self.file_stable_timestamps_check.isChecked()
        self.cfg.file_diarization_enabled = self.file_diarization_check.isChecked()
        self.cfg.file_speaker_count = str(self.file_speaker_count_combo.currentData() or "auto")
        self.cfg.save()
        if self.cfg.file_diarization_enabled and not self.models.is_installed("addon:sortformer"):
            self.status_label.setText("Для диаризации нужно загрузить дополнительную модель Sortformer во вкладке «Модели».")
        elif self.cfg.file_stable_timestamps_enabled and not self.models.is_installed("addon:vad"):
            self.status_label.setText("Для точных таймкодов нужно загрузить VAD-модель во вкладке «Модели».")

    def schedule_settings_autosave(self) -> None:
        if self._settings_loading:
            return
        self._settings_save_timer.start(350)

    def auto_save_settings(self) -> None:
        self.save_settings(auto=True)

    @staticmethod
    def _set_combo_value(combo: QComboBox, value) -> None:  # noqa: ANN001
        idx = combo.findData(value)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def ensure_selected_model_available(self) -> None:
        if self.models.is_available(self.cfg.selected_model):
            return
        log.info("Selected model is unavailable; fallback to default: %s -> %s", self.cfg.selected_model, DEFAULT_MODEL_KEY)
        self.cfg.selected_model = DEFAULT_MODEL_KEY
        self.cfg.save()

    def refresh_available_models_combo(self) -> None:
        current = self.cfg.selected_model if self.models.is_available(self.cfg.selected_model) else DEFAULT_MODEL_KEY
        for combo in [getattr(self, "model_combo", None), getattr(self, "file_model_combo", None)]:
            if combo is None:
                continue
            previous = combo.currentData() or current
            combo.blockSignals(True)
            combo.clear()
            for key in self.models.available_model_keys():
                spec = ALL_MODELS[key]
                combo.addItem(f"{spec.engine} — {spec.name}", key)
            self._set_combo_value(combo, previous if self.models.is_available(str(previous)) else current)
            combo.blockSignals(False)
        if hasattr(self, "model_combo"):
            self._set_combo_value(self.model_combo, current)

    def start_preload_selected_model(self) -> None:
        key = self.cfg.selected_model
        if key not in ALL_MODELS or not self.models.is_available(key) or not self.models.is_transcription_model(key):
            return
        if self.preload_worker and self.preload_worker.isRunning():
            self.status_label.setText("Предыдущая модель ещё подготавливается. Новая начнёт готовиться сразу после неё.")
            return
        cfg = AppConfig.load()
        self.preload_worker = PreloadWorker(self.models, key, cfg)
        self.preload_worker.finished_ok.connect(self.on_preload_done)
        self.preload_worker.failed.connect(self.on_preload_failed)
        self.preload_worker.start()
        self.status_label.setText(f"Подготавливаю модель: {model_display_name(key)}")

    def on_preload_done(self, key: str) -> None:
        log.info("Preload done: %s", key)
        if self.cfg.selected_model == key and not self.recorder.is_recording:
            self.status_label.setText(f"Готово. Модель подготовлена: {model_display_name(key)}")
        elif self.cfg.selected_model != key:
            self.start_preload_selected_model()

    def on_preload_failed(self, key: str, detail: str) -> None:
        log.error("Preload failed detail: %s", detail)
        if self.cfg.selected_model == key and not self.recorder.is_recording:
            self.status_label.setText("Не удалось заранее подготовить модель; запись всё равно можно запустить.")
        elif self.cfg.selected_model != key:
            self.start_preload_selected_model()

    def selected_table_model_key(self) -> str | None:
        rows = self.models_table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.models_table.item(rows[0].row(), 0)
        return None if item is None else item.data(Qt.UserRole)

    def refresh_models_table(self) -> None:
        self.models_table.setRowCount(0)
        for row, (key, spec) in enumerate(ALL_MODELS.items()):
            self.models_table.insertRow(row)
            engine_item = QTableWidgetItem(spec.engine)
            engine_item.setData(Qt.UserRole, key)
            self.models_table.setItem(row, 0, engine_item)
            self.models_table.setItem(row, 1, QTableWidgetItem(spec.name))
            self.models_table.setItem(row, 2, QTableWidgetItem(spec.language_hint))
            self.models_table.setItem(row, 3, QTableWidgetItem(spec.size_hint))
            status = self.model_status_overrides.get(key, self.models.installed_status(key))
            self.models_table.setItem(row, 4, QTableWidgetItem(status))
            self.models_table.setItem(row, 5, QTableWidgetItem(spec.note))
        self.models_table.resizeColumnsToContents()

    def update_model_status(self, key: str, status: str) -> None:
        self.model_status_overrides[key] = status
        for row in range(self.models_table.rowCount()):
            item = self.models_table.item(row, 0)
            if item is not None and item.data(Qt.UserRole) == key:
                self.models_table.setItem(row, 4, QTableWidgetItem(status))
                break

    def _set_hotkey_attention(self, enabled: bool, message: str | None = None) -> None:
        if not hasattr(self, "hotkey_edit"):
            return
        self.hotkey_edit.setProperty("needsAttention", enabled)
        self.hotkey_edit.style().unpolish(self.hotkey_edit)
        self.hotkey_edit.style().polish(self.hotkey_edit)
        self.hotkey_edit.update()
        if hasattr(self, "hotkey_hint_label"):
            if enabled:
                self.hotkey_hint_label.setText(message or "Нажмите поле выше и задайте другую комбинацию клавиш.")
                self.hotkey_hint_label.setStyleSheet("color: #fbbf24;")
            else:
                self.hotkey_hint_label.setText("Кликните поле и нажмите новую комбинацию. Если комбинация недоступна, поле подсветится.")
                self.hotkey_hint_label.setStyleSheet("")

    def _flash_button_state(self, button: QPushButton, text: str, *, kind: str = "info", seconds: int = 4) -> None:
        original_text = button.property("originalText") or button.text()
        if not button.property("originalText"):
            button.setProperty("originalText", original_text)
        if kind == "success":
            style = "background: #14532d; border-color: #22c55e; color: #f0fdf4;"
        elif kind == "error":
            style = "background: #451a1a; border-color: #ef4444; color: #fef2f2;"
        else:
            style = "background: #1e3a8a; border-color: #60a5fa; color: #eff6ff;"
        button.setText(text)
        button.setStyleSheet(style)

        def restore() -> None:
            button.setText(str(button.property("originalText") or original_text))
            button.setStyleSheet("")
            button.setEnabled(True)

        QTimer.singleShot(max(1, seconds) * 1000, restore)

    def _set_button_busy(self, button: QPushButton, text: str) -> None:
        if not button.property("originalText"):
            button.setProperty("originalText", button.text())
        button.setEnabled(False)
        button.setText(text)
        button.setStyleSheet("background: #1e3a8a; border-color: #60a5fa; color: #eff6ff;")

    def register_hotkey(self, show_errors: bool = True) -> bool:
        try:
            self.hotkey.start(self.cfg.hotkey, window_handle=int(self.winId()))
            backend = f" ({self.hotkey.backend})" if self.hotkey.backend else ""
            self.status_label.setText(f"Готово. Горячая клавиша: {self.cfg.hotkey}{backend}")
            self.overlay.set_hotkey(self.cfg.hotkey)
            self._set_hotkey_attention(False)
            return True
        except Exception as exc:  # noqa: BLE001
            log.exception("Hotkey registration failed")
            message = str(exc)
            self.status_label.setText(message)
            self._set_hotkey_attention(True, "Комбинация не зарегистрировалась. Нажмите поле и выберите другую, например Ctrl+Alt+Space.")
            if show_errors:
                QMessageBox.warning(self, "Горячая клавиша", message)
            return False

    def nativeEvent(self, event_type, message):  # noqa: ANN001
        try:
            if self.hotkey.handle_native_event(message):
                self.hotkey_signal.triggered.emit()
                return True, 0
        except Exception:  # noqa: BLE001
            log.exception("Native hotkey event handling failed")
        return super().nativeEvent(event_type, message)

    def register_cancel_hotkey(self) -> None:
        if self.cancel_hotkey_handle is not None:
            return
        try:
            import keyboard

            self.cancel_hotkey_handle = keyboard.add_hotkey("esc", lambda: self.cancel_signal.triggered.emit(), suppress=False, trigger_on_release=False)
        except Exception:  # noqa: BLE001
            log.exception("Esc cancel hotkey registration failed")
            self.cancel_hotkey_handle = None

    def unregister_cancel_hotkey(self) -> None:
        if self.cancel_hotkey_handle is None:
            return
        try:
            import keyboard

            keyboard.remove_hotkey(self.cancel_hotkey_handle)
        except Exception:
            pass
        self.cancel_hotkey_handle = None

    def _sync_overlay_visibility(self) -> None:
        self.overlay.set_hotkey(self.cfg.hotkey)
        if self.cfg.overlay_enabled:
            self.overlay.show_idle()
        else:
            self.overlay.hide_overlay()

    def open_logs_folder(self) -> None:
        path = logs_dir()
        try:
            if os.name == "nt":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:  # noqa: BLE001
            QMessageBox.information(self, "Логи", f"Папка логов: {path}\n\nНе удалось открыть автоматически: {exc}")

    def open_sound_settings(self) -> None:
        if os.name != "nt":
            QMessageBox.information(self, "Звук", "Быстрое открытие настроек звука доступно только в Windows.")
            return
        try:
            os.startfile("ms-settings:sound")  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            QMessageBox.information(self, "Звук", f"Откройте вручную: Параметры → Система → Звук.\n\nНе удалось открыть автоматически: {exc}")

    def open_microphone_privacy_settings(self) -> None:
        if os.name != "nt":
            QMessageBox.information(self, "Микрофон", "Быстрое открытие настроек микрофона доступно только в Windows.")
            return
        try:
            os.startfile("ms-settings:privacy-microphone")  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            QMessageBox.information(
                self,
                "Микрофон",
                "Откройте вручную: Параметры → Конфиденциальность и безопасность → Микрофон.\n\n"
                f"Не удалось открыть автоматически: {exc}",
            )

    def _mic_autodetect_running(self) -> bool:
        return bool(self.microphone_autodetect_worker and self.microphone_autodetect_worker.isRunning())

    def maybe_start_first_microphone_autodetect(self) -> None:
        if self.cfg.microphone_autodetect_done or self.cfg.audio_input_device_id:
            return
        self.start_microphone_autodetect(manual=False)

    def start_microphone_autodetect(self, *, manual: bool = False) -> None:
        if self._mic_autodetect_running():
            self.status_label.setText("Автонастройка микрофона уже выполняется…")
            if manual and hasattr(self, "autodetect_mic_btn"):
                self._flash_button_state(self.autodetect_mic_btn, "Уже выполняется…", kind="info", seconds=3)
            return
        if self.recorder.is_recording or (self.transcribe_worker and self.transcribe_worker.isRunning()) or self._file_job_running():
            if manual and hasattr(self, "autodetect_mic_btn"):
                self._flash_button_state(self.autodetect_mic_btn, "Занято", kind="error", seconds=4)
            self.status_label.setText("Дождитесь завершения текущей записи или расшифровки файла.")
            return
        self.status_label.setText("Автонастройка микрофона: проверяю доступные устройства…")
        if manual and hasattr(self, "autodetect_mic_btn"):
            self._set_button_busy(self.autodetect_mic_btn, "Ищем микрофон…")
        self.microphone_autodetect_worker = MicrophoneAutodetectWorker(AppConfig.load())
        self.microphone_autodetect_worker.message.connect(self.on_microphone_autodetect_message)
        self.microphone_autodetect_worker.finished_ok.connect(lambda result, manual=manual: self.on_microphone_autodetect_done(result, manual=manual))
        self.microphone_autodetect_worker.failed.connect(lambda detail, manual=manual: self.on_microphone_autodetect_failed(detail, manual=manual))
        self.microphone_autodetect_worker.start()

    def on_microphone_autodetect_message(self, message: str) -> None:
        self.status_label.setText(message)

    def on_microphone_autodetect_done(self, result: object, *, manual: bool = False) -> None:
        if not isinstance(result, MicrophoneAutodetectResult):
            return
        self.cfg.audio_input_device_id = result.device_id
        if result.sample_rate > 0:
            self.cfg.sample_rate = result.sample_rate
        self.cfg.microphone_autodetect_done = True
        self.cfg.save()
        self.recorder = AudioRecorder(sample_rate=self.cfg.sample_rate, input_device_id=self.cfg.audio_input_device_id, meeting_compatibility=self.cfg.audio_meeting_compatibility)
        self.refresh_microphone_combo()
        self._set_combo_value(self.microphone_combo, self.cfg.audio_input_device_id)
        msg = f"Найден рабочий микрофон: {result.label}"
        if result.rms_level <= 0.0001:
            msg += " · устройство открывается, но тестовый уровень был очень тихим"
        self.status_label.setText(msg)
        if manual and hasattr(self, "autodetect_mic_btn"):
            self._flash_button_state(self.autodetect_mic_btn, f"Найден ✓", kind="success", seconds=4)
            log.info("Microphone autodetect UI state: success shown")
        log.info("Microphone autodetect selected: id=%s label=%s rate=%s rms=%.6f", result.device_id, result.label, result.sample_rate, result.rms_level)

    def on_microphone_autodetect_failed(self, detail: str, *, manual: bool = False) -> None:
        self.cfg.microphone_autodetect_done = False
        self.cfg.save()
        self.status_label.setText("Автонастройка микрофона не нашла доступное устройство. Подробности записаны в логи.")
        log.error("Microphone autodetect failed detail: %s", detail)
        if manual and hasattr(self, "autodetect_mic_btn"):
            self._flash_button_state(self.autodetect_mic_btn, "Не найден", kind="error", seconds=5)
            log.info("Microphone autodetect UI state: error shown")

    def test_microphone_access(self) -> None:
        if self.recorder.is_recording:
            self.status_label.setText("Сейчас уже идёт запись. Остановите её перед проверкой микрофона.")
            if hasattr(self, "test_mic_btn"):
                self._flash_button_state(self.test_mic_btn, "Идёт запись", kind="error", seconds=4)
            return
        if self.transcribe_worker and self.transcribe_worker.isRunning():
            self.status_label.setText("Дождитесь окончания расшифровки перед проверкой микрофона.")
            if hasattr(self, "test_mic_btn"):
                self._flash_button_state(self.test_mic_btn, "Занято", kind="error", seconds=4)
            return
        try:
            if hasattr(self, "test_mic_btn"):
                self._set_button_busy(self.test_mic_btn, "Проверяем…")
            self.save_settings(auto=True)
            recorder = AudioRecorder(sample_rate=self.cfg.sample_rate, input_device_id=self.cfg.audio_input_device_id, meeting_compatibility=self.cfg.audio_meeting_compatibility)
            recorder.start()
            self.microphone_test_recorder = recorder
            selected = self.microphone_combo.currentText() if hasattr(self, "microphone_combo") else "выбранный микрофон"
            self.status_label.setText(f"Микрофон доступен: {selected}. Windows должна зафиксировать обращение к микрофону.")
            if hasattr(self, "test_mic_btn"):
                self._flash_button_state(self.test_mic_btn, "Микрофон работает ✓", kind="success", seconds=4)
                log.info("Microphone test UI state: success shown")
            log.info("Microphone test succeeded: %s", selected)

            def finish_test() -> None:
                try:
                    recorder.cancel()
                except Exception as exc:  # noqa: BLE001
                    log.warning("Microphone test cleanup failed: %s", exc)
                if self.microphone_test_recorder is recorder:
                    self.microphone_test_recorder = None

            QTimer.singleShot(700, finish_test)
        except Exception as exc:  # noqa: BLE001
            log.exception("Microphone test failed")
            self.status_label.setText(f"Микрофон недоступен: {exc}")
            if hasattr(self, "test_mic_btn"):
                self._flash_button_state(self.test_mic_btn, "Недоступен", kind="error", seconds=5)
                log.info("Microphone test UI state: error shown")

    def on_dictation_model_changed(self) -> None:
        if self._settings_loading or not hasattr(self, "model_combo"):
            return
        key = str(self.model_combo.currentData() or "")
        if not key:
            return
        if not self.models.is_available(key):
            self.status_label.setText("Эта модель ещё не загружена. Выберите загруженную модель или загрузите её во вкладке «Модели».")
            self.refresh_available_models_combo()
            return
        if self.cfg.selected_model == key:
            return
        self.cfg.selected_model = key
        self.cfg.save()
        self.refresh_available_models_combo()
        self.status_label.setText(f"Активная модель: {model_display_name(key)}. Подготавливаю в фоне…")
        log.info("Active model changed from combo: %s", key)
        self.start_preload_selected_model()

    def use_selected_model(self) -> None:
        key = str(self.model_combo.currentData())
        if not self.models.is_available(key):
            QMessageBox.information(self, "Модели", "Эта модель ещё не загружена. Сначала загрузите её во вкладке «Модели».")
            self.refresh_available_models_combo()
            return
        self.cfg.selected_model = key
        self.cfg.save()
        self.status_label.setText(f"Активная модель: {model_display_name(key)}")
        self.start_preload_selected_model()

    def use_selected_model_from_table(self) -> None:
        key = self.selected_table_model_key()
        if not key:
            QMessageBox.information(self, "Модели", "Сначала выберите модель.")
            return
        if not self.models.is_transcription_model(key):
            QMessageBox.information(self, "Модели", "Это дополнительная модель для функций файлов. Её нельзя выбрать как активную модель диктовки.")
            return
        if not self.models.is_available(key):
            QMessageBox.information(self, "Модели", "Эта модель ещё не загружена. Активной можно сделать только загруженную модель.")
            return
        self.cfg.selected_model = key
        self.cfg.save()
        self.refresh_available_models_combo()
        self._set_combo_value(self.model_combo, key)
        self.tabs.setCurrentIndex(0)
        self.status_label.setText(f"Активная модель: {model_display_name(key)}")
        self.start_preload_selected_model()

    def download_selected_model(self) -> None:
        key = self.selected_table_model_key()
        if not key:
            QMessageBox.information(self, "Модели", "Сначала выберите модель.")
            return
        if key in self.downloading_keys:
            QMessageBox.information(self, "Модели", "Эта модель уже загружается. Дождитесь завершения загрузки.")
            return
        self.downloading_keys.add(key)
        self.download_btn.setEnabled(False)
        self.delete_btn.setEnabled(False)
        self.update_model_status(key, "Загрузка…")
        self.download_worker = DownloadWorker(self.models, key, self.cfg)
        self.download_worker.message.connect(lambda msg, model_key=key: self.on_download_message(model_key, msg))
        self.download_worker.finished_ok.connect(self.on_download_done)
        self.download_worker.failed.connect(lambda detail, model_key=key: self.on_download_failed(model_key, detail))
        self.download_worker.start()

    def on_download_message(self, key: str, message: object) -> None:
        if isinstance(message, DownloadProgress):
            self.download_progress_tick += 1
            frame = self.download_progress_frames[self.download_progress_tick % len(self.download_progress_frames)]
            if message.percent is None:
                table_status = f"{frame} Загрузка…"
            else:
                table_status = f"{frame} {message.percent}% Загрузка…"
            self.status_label.setText(message.message)
            self.update_model_status(key, table_status)
            return
        text = str(message)
        self.status_label.setText(text)
        self.download_progress_tick += 1
        frame = self.download_progress_frames[self.download_progress_tick % len(self.download_progress_frames)]
        self.update_model_status(key, f"{frame} Загрузка… " + text[:60])

    def on_download_done(self, key: str) -> None:
        self.downloading_keys.discard(key)
        self.download_btn.setEnabled(True)
        self.delete_btn.setEnabled(True)
        self.model_status_overrides.pop(key, None)
        self.refresh_models_table()
        self.refresh_available_models_combo()
        self.status_label.setText(f"Загружено: {model_display_name(key)}")

    def on_download_failed(self, key: str, detail: str) -> None:
        self.downloading_keys.discard(key)
        self.download_btn.setEnabled(True)
        self.delete_btn.setEnabled(True)
        self.update_model_status(key, "Ошибка загрузки")
        log.error("Download failed detail: %s", detail)
        QMessageBox.critical(self, "Ошибка загрузки", detail)
        self.status_label.setText("Загрузка модели не удалась. Подробности записаны в логи.")

    def delete_selected_model(self) -> None:
        key = self.selected_table_model_key()
        if not key:
            QMessageBox.information(self, "Модели", "Сначала выберите модель.")
            return
        if self._file_job_running():
            QMessageBox.information(self, "Модели", "Нельзя удалять модели, пока идёт расшифровка файла.")
            return
        if key in self.downloading_keys:
            QMessageBox.information(self, "Модели", "Нельзя удалить модель, пока она загружается. Дождитесь завершения или перезапустите приложение.")
            return
        result = QMessageBox.question(self, "Удалить модель", f"Удалить локальные файлы модели {model_display_name(key)}?")
        if result != QMessageBox.Yes:
            return
        try:
            self.models.delete(key)
            self.model_status_overrides.pop(key, None)
            if self.cfg.selected_model == key and not self.models.is_available(key):
                self.cfg.selected_model = DEFAULT_MODEL_KEY
                self.cfg.save()
            self.refresh_models_table()
            self.refresh_available_models_combo()
            self.status_label.setText("Файлы модели удалены.")
        except Exception as exc:  # noqa: BLE001
            log.exception("Delete model failed: %s", key)
            self.refresh_models_table()
            QMessageBox.critical(self, "Удаление модели", str(exc))

    def choose_audio_file(self) -> None:
        patterns = " ".join(f"*{ext}" for ext in sorted(SUPPORTED_AUDIO_EXTENSIONS))
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Выберите аудио или видео файл",
            "",
            f"Аудио и видео ({patterns});;Все файлы (*.*)",
        )
        if not file_path:
            return
        path = Path(file_path)
        if not is_supported_audio_file(path):
            QMessageBox.warning(self, "Файл", "Формат файла пока не поддерживается. Выберите wav, mp3, m4a, mp4, webm, ogg или flac.")
            return
        self.selected_file_path = path
        self.file_path_edit.setText(str(path))
        self.file_status_label.setText(f"Выбран файл: {path.name}")
        self.file_result_text.clear()
        self._file_transcript_blocks = []
        self.file_copy_btn.setEnabled(False)

    def _file_job_running(self) -> bool:
        return bool(self.file_transcribe_worker and self.file_transcribe_worker.isRunning())

    def start_file_transcription(self) -> None:
        if self.recorder.is_recording or (self.transcribe_worker and self.transcribe_worker.isRunning()) or self._mic_autodetect_running():
            QMessageBox.information(self, "Файлы", "Сейчас идёт диктовка, распознавание голоса или автонастройка микрофона. Завершите текущую операцию перед расшифровкой файла.")
            return
        if self._file_job_running():
            QMessageBox.information(self, "Файлы", "Файл уже расшифровывается. Дождитесь завершения или нажмите «Отменить».")
            return
        path = self.selected_file_path
        if path is None:
            raw_path = self.file_path_edit.text().strip()
            path = Path(raw_path) if raw_path else None
        if path is None or not path.exists():
            QMessageBox.information(self, "Файлы", "Сначала выберите существующий аудио или видео файл.")
            return
        if not is_supported_audio_file(path):
            QMessageBox.warning(self, "Файл", "Формат файла пока не поддерживается. Выберите wav, mp3, m4a, mp4, webm, ogg или flac.")
            return
        key = str(self.file_model_combo.currentData() or self.cfg.selected_model)
        if not self.models.is_available(key):
            QMessageBox.information(self, "Модели", "Выбранная модель ещё не загружена. Сначала загрузите её во вкладке «Модели».")
            self.refresh_available_models_combo()
            return
        self.save_settings(auto=True)
        self.save_file_options()
        if self.file_stable_timestamps_check.isChecked() and not self.models.is_installed("addon:vad"):
            self.tabs.setCurrentIndex(2)
            QMessageBox.information(self, "Точные таймкоды", "Сначала загрузите дополнительную модель «VAD для точных таймкодов» во вкладке «Модели».")
            return
        if self.file_diarization_check.isChecked() and not self.models.is_installed("addon:sortformer"):
            self.tabs.setCurrentIndex(2)
            QMessageBox.information(self, "Диаризация", "Сначала загрузите дополнительную модель «Sortformer Diarization v2.1» во вкладке «Модели».")
            return

        self.file_cancel_requested = False
        self._file_transcript_blocks = []
        self._file_show_timestamps_for_job = self.file_stable_timestamps_check.isChecked()
        self.file_result_text.clear()
        self.file_copy_btn.setEnabled(False)
        self.file_transcribe_btn.setEnabled(False)
        self.file_cancel_btn.setEnabled(True)
        self.choose_file_btn.setEnabled(False)
        self.file_model_combo.setEnabled(False)
        self.file_progress.setVisible(True)
        self.file_progress.setRange(0, 100)
        self.file_progress.setValue(0)
        self.file_progress.setFormat("0% · подготовка…")
        self.file_status_label.setText("Подготавливаю файл…")
        self.status_label.setText("Идёт расшифровка файла. Диктовка временно недоступна.")
        if self.cfg.overlay_enabled:
            self.overlay.show_processing()

        cfg = AppConfig.load()
        self.file_transcribe_worker = FileTranscribeWorker(self.models, key, path, cfg)
        self.file_transcribe_worker.message.connect(self.on_file_transcription_message)
        self.file_transcribe_worker.progress.connect(self.on_file_transcription_progress)
        self.file_transcribe_worker.block_ready.connect(self.on_file_transcription_block)
        self.file_transcribe_worker.finished_text.connect(self.on_file_transcription_done)
        self.file_transcribe_worker.failed.connect(self.on_file_transcription_failed)
        self.file_transcribe_worker.cancelled.connect(self.on_file_transcription_cancelled)
        self.file_transcribe_worker.start()
        log.info("File transcription started: model=%s path=%s", key, path)

    def on_file_transcription_message(self, message: str) -> None:
        self.file_status_label.setText(message)
        self.file_progress.setFormat(message)
        self.status_label.setText(message)

    def on_file_transcription_progress(self, progress: object) -> None:
        if not isinstance(progress, FileProgress):
            return
        self.file_progress.setRange(0, 100)
        self.file_progress.setValue(progress.percent)
        self.file_progress.setFormat(progress.message)
        self.file_status_label.setText(progress.message)
        self.status_label.setText(progress.message)
        if self.cfg.overlay_enabled:
            self.overlay.show_processing(f"Файл · {progress.percent}%")

    def on_file_transcription_block(self, block: object) -> None:
        if not isinstance(block, FileTranscriptBlock):
            return
        text = block.text.strip()
        if not text:
            return
        speaker = (block.speaker or "").strip()
        if block.replace_existing:
            self._file_transcript_blocks = []
            self.file_result_text.clear()
        prefix = ""
        if self._file_show_timestamps_for_job:
            prefix += f"[{format_duration(block.start_seconds)}–{format_duration(block.end_seconds)}] "
        if speaker:
            prefix += f"{speaker}: "
        self._file_transcript_blocks.append({
            "start": round(float(block.start_seconds), 3),
            "end": round(float(block.end_seconds), 3),
            "speaker": speaker,
            "text": text,
        })
        current = self.file_result_text.toPlainText().strip()
        addition = prefix + text
        if current:
            self.file_result_text.append(addition)
        else:
            self.file_result_text.setPlainText(addition)
        self.file_result_text.moveCursor(QTextCursor.End)
        self.file_copy_btn.setEnabled(True)

    def cancel_file_transcription(self) -> None:
        if not self._file_job_running():
            return
        self.file_cancel_requested = True
        if self.file_transcribe_worker is not None:
            self.file_transcribe_worker.cancel()
        self.file_cancel_btn.setEnabled(False)
        self.file_status_label.setText("Отмена запрошена. Дождитесь завершения фонового процесса…")
        self.file_progress.setFormat("Отмена…")
        self.status_label.setText("Отмена расшифровки файла запрошена. Результат будет проигнорирован.")

    def _reset_file_transcription_ui(self) -> None:
        self.file_transcribe_btn.setEnabled(True)
        self.file_cancel_btn.setEnabled(False)
        self.choose_file_btn.setEnabled(True)
        self.file_model_combo.setEnabled(True)
        self.file_progress.setRange(0, 100)
        self.file_progress.setValue(0)
        self.file_progress.setVisible(False)
        if self.cfg.overlay_enabled and not self.recorder.is_recording:
            QTimer.singleShot(900, self.overlay.show_idle)

    def on_file_transcription_done(self, text: str, duration: float, file_path: str, model_key: str) -> None:
        if self.file_cancel_requested:
            self.on_file_transcription_cancelled()
            return
        text = text.strip()
        if not self.file_result_text.toPlainText().strip():
            self.file_result_text.setPlainText(text)
        self.file_copy_btn.setEnabled(bool(self.file_result_text.toPlainText().strip() or text))
        self.last_text.setPlainText(text)
        if text:
            path = Path(file_path)
            segments_json = json.dumps(self._file_transcript_blocks, ensure_ascii=False) if self._file_transcript_blocks else ""
            self.history.add(model_key, duration, False, text, source="file", file_name=path.name, file_path=str(path), segments_json=segments_json)
            self.refresh_history()
            self.file_status_label.setText("Готово: файл расшифрован и сохранён в истории. Текст не скопирован в буфер автоматически.")
            self.status_label.setText("Файл расшифрован. Нажмите «Скопировать результат», если хотите поместить текст в буфер обмена.")
        else:
            self.file_status_label.setText("Расшифровка завершилась, но текст не найден.")
            self.status_label.setText("Файл обработан, но текст не найден.")
        self.file_progress.setValue(100)
        self.file_progress.setFormat("100% · готово")
        self._reset_file_transcription_ui()
        log.info("File transcription done: model=%s path=%s chars=%s", model_key, file_path, len(text))

    def on_file_transcription_failed(self, detail: str) -> None:
        if self.file_cancel_requested:
            self.on_file_transcription_cancelled()
            return
        log.error("File transcription failed detail: %s", detail)
        self.file_status_label.setText("Ошибка расшифровки файла. Подробности записаны в логи.")
        self.status_label.setText("Ошибка расшифровки файла.")
        if self.cfg.overlay_enabled:
            self.overlay.show_error("Ошибка", seconds=5)
        self._reset_file_transcription_ui()
        QMessageBox.critical(self, "Расшифровка файла", detail)

    def on_file_transcription_cancelled(self) -> None:
        self.file_cancel_requested = False
        self.file_status_label.setText("Расшифровка файла отменена. История и буфер обмена не изменены.")
        self.status_label.setText("Расшифровка файла отменена.")
        if self.cfg.overlay_enabled:
            self.overlay.show_cancelled(seconds=3)
        self._reset_file_transcription_ui()
        log.info("File transcription cancelled")

    def save_settings(self, *, auto: bool = False) -> None:
        old_hotkey = self.cfg.hotkey
        old_model_runtime = (self.cfg.language, self.cfg.device, self.cfg.compute_type)
        seq = self.hotkey_edit.keySequence().toString(QKeySequence.NativeText)
        requested_hotkey = normalize_hotkey(seq) if seq else old_hotkey
        self.cfg.auto_paste = self.auto_paste_check.isChecked()
        self.cfg.paste_only_when_text_field_detected = self.detect_text_field_check.isChecked()
        self.cfg.audio_meeting_compatibility = self.meeting_compat_check.isChecked()
        self.cfg.overlay_enabled = self.overlay_enabled_check.isChecked()
        self.cfg.autostart_enabled = self.autostart_check.isChecked() and autostart.is_supported()
        self.cfg.audio_input_device_id = str(self.microphone_combo.currentData() or "")
        self.cfg.hf_token = self.hf_token_edit.text().strip()
        self.cfg.updates_enabled = self.updates_enabled_check.isChecked()
        self.cfg.update_repo = normalize_repo(self.update_repo_edit.text())
        if self.update_repo_edit.text().strip() and self.update_repo_edit.text().strip() != self.cfg.update_repo:
            self.update_repo_edit.setText(self.cfg.update_repo)
        if hasattr(self, "file_stable_timestamps_check"):
            self.cfg.file_stable_timestamps_enabled = self.file_stable_timestamps_check.isChecked()
            self.cfg.file_diarization_enabled = self.file_diarization_check.isChecked()
            self.cfg.file_speaker_count = str(self.file_speaker_count_combo.currentData() or "auto")
        self.cfg.live_transcription = False
        self.cfg.live_insert_confirmed_text = False
        self.cfg.language = str(self.language_combo.currentData())
        self.cfg.device = str(self.device_combo.currentData())
        self.cfg.compute_type = str(self.compute_combo.currentData())
        hotkey_error = None
        if requested_hotkey != old_hotkey:
            # Do not persist a broken hotkey. The HotkeyService keeps the old
            # handle active if the new combo cannot be parsed/registered.
            previous = self.cfg.hotkey
            self.cfg.hotkey = requested_hotkey
            if not self.register_hotkey(show_errors=not auto):
                hotkey_error = requested_hotkey
                self.cfg.hotkey = previous
                self.overlay.set_hotkey(self.cfg.hotkey)
        else:
            self.overlay.set_hotkey(self.cfg.hotkey)
            self._set_hotkey_attention(False)
        autostart_error = None
        if autostart.is_supported():
            try:
                autostart.set_enabled(self.cfg.autostart_enabled)
            except Exception as exc:  # noqa: BLE001
                log.exception("Autostart update failed")
                autostart_error = exc
                self.autostart_check.setChecked(False)
                self.cfg.autostart_enabled = False
        self.cfg.save()
        self._sync_overlay_visibility()
        if old_model_runtime != (self.cfg.language, self.cfg.device, self.cfg.compute_type):
            self.start_preload_selected_model()
        if hotkey_error is not None:
            self.status_label.setText("Горячая клавиша не сохранена. Поле подсвечено — выберите другую комбинацию.")
        elif autostart_error is not None:
            self.status_label.setText(f"Настройки сохранены, но автозагрузку изменить не удалось: {autostart_error}")
        else:
            self.status_label.setText("Настройки сохранены автоматически." if auto else "Настройки сохранены.")


    def _update_repo_ready(self, manual: bool) -> str:
        repo = normalize_repo(self.update_repo_edit.text() if hasattr(self, "update_repo_edit") else self.cfg.update_repo)
        if not repo:
            if manual:
                QMessageBox.information(
                    self,
                    "Обновления",
                    "Укажите GitHub repo в настройках в формате owner/repo. Например: your-org/voice-input-local.",
                )
            return ""
        return repo

    def check_for_updates(self, *, manual: bool = False) -> None:
        if manual:
            self.save_settings(auto=True)
        if not manual and not self.cfg.updates_enabled:
            return
        repo = self._update_repo_ready(manual)
        if not repo:
            return
        if self.update_check_worker and self.update_check_worker.isRunning():
            if manual:
                self.status_label.setText("Проверка обновлений уже выполняется…")
            return
        if hasattr(self, "check_updates_btn") and manual:
            self._set_button_busy(self.check_updates_btn, "Проверяю…")
        self.status_label.setText("Проверяю обновления…")
        log.info("Update check started: repo=%s current=%s manual=%s", repo, __version__, manual)
        self.update_check_worker = UpdateCheckWorker(repo, __version__)
        self.update_check_worker.finished_result.connect(lambda info, m=manual: self.on_update_check_result(info, manual=m))
        self.update_check_worker.failed.connect(lambda detail, m=manual: self.on_update_check_failed(detail, manual=m))
        self.update_check_worker.start()

    def on_update_check_result(self, info: object, *, manual: bool) -> None:
        self.cfg.last_update_check_ts = time.time()
        self.cfg.save()
        if info is None:
            self.status_label.setText("Установлена последняя версия." if manual else "Готово")
            if manual and hasattr(self, "check_updates_btn"):
                self._flash_button_state(self.check_updates_btn, "Обновлений нет ✓", kind="success", seconds=4)
            return
        if not isinstance(info, UpdateInfo):
            self.status_label.setText("Неожиданный ответ проверки обновлений.")
            if manual and hasattr(self, "check_updates_btn"):
                self._flash_button_state(self.check_updates_btn, "Ошибка", kind="error", seconds=4)
            return
        self.pending_update_info = info
        self.status_label.setText(f"Доступна версия {info.latest_version}.")
        if hasattr(self, "check_updates_btn"):
            self._flash_button_state(self.check_updates_btn, f"Доступна {info.latest_version}", kind="info", seconds=4)
        notes = info.release_notes.strip()
        if len(notes) > 900:
            notes = notes[:900].rstrip() + "…"
        message = (
            f"Доступна новая версия Voice Input Local {info.latest_version}.\n"
            f"Текущая версия: {info.current_version}.\n\n"
            f"Файл: {info.asset_name}\n\n"
            "Скачать и запустить обновление сейчас?"
        )
        if notes:
            message += f"\n\nОписание релиза:\n{notes}"
        answer = QMessageBox.question(self, "Обновление доступно", message, QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if answer == QMessageBox.Yes:
            self.download_update(info)

    def on_update_check_failed(self, detail: str, *, manual: bool) -> None:
        log.error("Update check failed detail: %s", detail)
        self.status_label.setText("Не удалось проверить обновления.")
        if manual and hasattr(self, "check_updates_btn"):
            self._flash_button_state(self.check_updates_btn, "Ошибка проверки", kind="error", seconds=5)
            QMessageBox.warning(self, "Обновления", detail)

    def download_update(self, info: UpdateInfo) -> None:
        if self.update_download_worker and self.update_download_worker.isRunning():
            self.status_label.setText("Обновление уже скачивается…")
            return
        if hasattr(self, "check_updates_btn"):
            self._set_button_busy(self.check_updates_btn, "Скачиваю…")
        self.status_label.setText(f"Скачиваю обновление {info.latest_version}…")
        self.update_download_worker = UpdateDownloadWorker(info)
        self.update_download_worker.progress.connect(self.on_update_download_progress)
        self.update_download_worker.finished_path.connect(lambda path: self.on_update_download_done(path, info))
        self.update_download_worker.failed.connect(self.on_update_download_failed)
        self.update_download_worker.start()

    def on_update_download_progress(self, done: int, total: int) -> None:
        if total > 0:
            percent = int(max(0, min(100, done * 100 / total)))
            self.status_label.setText(f"Скачиваю обновление… {percent}%")
            if hasattr(self, "check_updates_btn"):
                self.check_updates_btn.setText(f"Скачиваю… {percent}%")
        else:
            self.status_label.setText(f"Скачиваю обновление… {done // (1024 * 1024)} МБ")

    def on_update_download_done(self, path: str, info: UpdateInfo) -> None:
        self.status_label.setText("Обновление скачано.")
        if hasattr(self, "check_updates_btn"):
            self._flash_button_state(self.check_updates_btn, "Скачано ✓", kind="success", seconds=4)
        if info.is_installer:
            answer = QMessageBox.question(
                self,
                "Обновление скачано",
                "Установщик обновления скачан. Запустить его сейчас?\n\nПриложение закроется, чтобы установщик мог заменить файлы.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if answer == QMessageBox.Yes:
                try:
                    launch_update_file(Path(path))
                    self.really_quit()
                except Exception as exc:  # noqa: BLE001
                    QMessageBox.warning(self, "Обновления", f"Не удалось запустить установщик: {exc}")
        else:
            QMessageBox.information(
                self,
                "Обновление скачано",
                f"Portable-архив скачан сюда:\n{path}\n\nРаспакуйте его вручную после закрытия приложения.",
            )

    def on_update_download_failed(self, detail: str) -> None:
        log.error("Update download failed detail: %s", detail)
        self.status_label.setText("Не удалось скачать обновление.")
        if hasattr(self, "check_updates_btn"):
            self._flash_button_state(self.check_updates_btn, "Ошибка скачивания", kind="error", seconds=5)
        QMessageBox.warning(self, "Обновления", detail)


    def toggle_recording(self) -> None:
        log.info("Toggle recording requested. recorder_active=%s transcribe_running=%s", self.recorder.is_recording, bool(self.transcribe_worker and self.transcribe_worker.isRunning()))
        if self._file_job_running():
            self.status_label.setText("Идёт расшифровка файла. Диктовка временно недоступна.")
            if self.cfg.overlay_enabled:
                self.overlay.show_processing()
            return
        if self._mic_autodetect_running():
            self.status_label.setText("Идёт автонастройка микрофона. Диктовка временно недоступна.")
            return
        if self.transcribe_worker and self.transcribe_worker.isRunning():
            return
        if self.recorder.is_recording:
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self) -> None:
        try:
            self.save_settings(auto=True)
            self.cfg = AppConfig.load()
            # v3.4: keep the shipped app stable. Live transcription is disabled
            # until a proper streaming pipeline is implemented.
            self.cfg.live_transcription = False
            if not self.models.is_available(self.cfg.selected_model):
                QMessageBox.information(self, "Модели", "Активная модель не загружена. Выберите загруженную модель во вкладке «Диктовка» или загрузите её во вкладке «Модели».")
                self.ensure_selected_model_available()
                self.refresh_available_models_combo()
                return
            self.result_preview_active = False
            self.result_preview_text = ""
            self.unregister_cancel_hotkey()
            self.recorder = AudioRecorder(sample_rate=self.cfg.sample_rate, input_device_id=self.cfg.audio_input_device_id, meeting_compatibility=self.cfg.audio_meeting_compatibility)
            # Only the main Voice Input Local window should count as "own window".
            # The floating overlay is also a window in this process; after dragging
            # it, Windows can report the overlay as foreground. Treating that as
            # own_window suppressed the no-target-field transcript preview.
            try:
                self.recording_started_in_own_window = foreground_matches_window_handle(int(self.winId()))
            except Exception:
                self.recording_started_in_own_window = foreground_belongs_to_current_process()
            self.recorder.start()
            self.cancel_requested = False
            self.live_last_request_at = 0.0
            self.live_processed_until_seconds = 0.0
            self.live_total_text = ""
            self.live_had_insertions = False
            self.live_unavailable_notice_shown = False
            self.pending_final = None
            self.live_target_is_text_field = (focused_control_accepts_text() is True) and not self.recording_started_in_own_window
            self.register_cancel_hotkey()
            self.toggle_btn.setText("Остановить запись")
            self.status_label.setText("Идёт запись… Esc отменит запись, горячая клавиша остановит и запустит расшифровку.")
            if self.cfg.overlay_enabled:
                self.overlay.reset_for_new_recording(live_enabled=self.cfg.live_transcription)
                self.overlay.show_recording(0.0, live_enabled=self.cfg.live_transcription)
            self.update_recording_badge()
            log.info("Recording started. live_target_is_text_field=%s own_window=%s model=%s", self.live_target_is_text_field, self.recording_started_in_own_window, self.cfg.selected_model)
        except Exception as exc:  # noqa: BLE001
            log.exception("Recording start failed")
            self.toggle_btn.setText("Начать запись")
            self.status_label.setText("Запись недоступна. Подробности записаны в логи.")
            if self.cfg.overlay_enabled:
                self.overlay.show_error("Ошибка", seconds=5)
            QMessageBox.critical(self, "Запись недоступна", str(exc))

    def stop_recording(self) -> None:
        try:
            wav_path, duration = self.recorder.stop_to_wav()
            if duration < 1.0:
                self._cleanup_wav(wav_path)
                self.toggle_btn.setText("Начать запись")
                self.unregister_cancel_hotkey()
                self.update_recording_badge()
                if self.cfg.overlay_enabled:
                    self.overlay.show_cancelled(seconds=3)
                self.status_label.setText("Запись слишком короткая. Скажите фразу дольше 1 секунды и попробуйте снова.")
                QMessageBox.information(self, "Запись", "Запись слишком короткая или речь не обнаружена. Попробуйте ещё раз.")
                return
        except Exception as exc:  # noqa: BLE001
            log.exception("Recording stop failed")
            QMessageBox.critical(self, "Запись", str(exc))
            self.toggle_btn.setText("Начать запись")
            self.unregister_cancel_hotkey()
            return
        self.toggle_btn.setEnabled(True)
        self.toggle_btn.setText("Начать запись")
        self.record_badge.setText("Распознаю")
        self.status_label.setText("Финальная расшифровка локально…")
        if self.cfg.overlay_enabled:
            self.overlay.show_processing()
        cfg = AppConfig.load()
        # Start the final pass immediately. Live results that arrive after stop are ignored;
        # the final pass is the source of truth and does not consume live text.
        self.pending_final = None
        self._begin_final_transcription(wav_path, duration, cfg)

    def cancel_current_action(self) -> None:
        # Special case: final transcript preview is visible because no target
        # text field was detected. Esc should close the preview, not delete the
        # already saved transcript.
        if self.result_preview_active:
            self.result_preview_active = False
            self.result_preview_text = ""
            if self.cfg.overlay_enabled:
                self.overlay.show_idle()
            self.unregister_cancel_hotkey()
            self.status_label.setText("Готово. Текст сохранён в истории и буфере обмена.")
            return

        if not self.recorder.is_recording and not (self.transcribe_worker and self.transcribe_worker.isRunning()) and not (self.live_worker and self.live_worker.isRunning()):
            return
        self.cancel_requested = True
        if self.pending_final is not None:
            self._cleanup_wav(self.pending_final[0])
        self.pending_final = None
        final_running = bool(self.transcribe_worker and self.transcribe_worker.isRunning())
        if self.recorder.is_recording:
            self.recorder.cancel()
        self.toggle_btn.setText("Начать запись")
        self.toggle_btn.setEnabled(not final_running)
        self.record_badge.setText("Отменено")
        self.record_badge.setStyleSheet("background: #3f3f46; color: white; border-radius: 14px; padding: 8px 12px;")
        if final_running:
            self.status_label.setText("Отменено. Дожидаюсь завершения фонового процесса; результат будет проигнорирован.")
        else:
            self.status_label.setText("Отменено. История и буфер обмена не изменены.")
        if self.cfg.overlay_enabled:
            self.overlay.show_cancelled(seconds=4)
        self.unregister_cancel_hotkey()
        log.info("Current recording/transcription cancelled by user")

    def _begin_final_transcription(self, wav_path: Path, duration: float, cfg: AppConfig) -> None:
        self.pending_final = None
        self.transcribe_worker = TranscribeWorker(self.models, cfg.selected_model, wav_path, duration, cfg, is_live=False)
        self.transcribe_worker.finished_text.connect(lambda text, dur: self.on_transcription_done(text, dur, wav_path))
        self.transcribe_worker.failed.connect(lambda detail, path=wav_path: self.on_transcription_failed(detail, path))
        self.transcribe_worker.start()

    def on_transcription_done(self, text: str, duration: float, wav_path: Path) -> None:
        if self.cancel_requested:
            self._cleanup_wav(wav_path)
            self.update_recording_badge()
            self.unregister_cancel_hotkey()
            self.toggle_btn.setEnabled(True)
            self.status_label.setText("Расшифровка отменена. Результат проигнорирован.")
            return
        text = text.strip()
        self.last_text.setPlainText(text)
        inserted = False
        show_overlay_result = False
        if text:
            should_try_paste = self.cfg.auto_paste and self.live_target_is_text_field and not self.recording_started_in_own_window
            if should_try_paste:
                inserted = copy_and_maybe_paste(
                    text,
                    auto_paste=True,
                    only_when_text_field_detected=self.cfg.paste_only_when_text_field_detected,
                    allow_current_process=False,
                )
            if not inserted:
                pyperclip.copy(text)
                show_overlay_result = not self.recording_started_in_own_window
            self.history.add(self.cfg.selected_model, duration, inserted, text)
        self._cleanup_wav(wav_path)
        self.refresh_history()
        self.update_recording_badge()
        self.toggle_btn.setEnabled(True)
        self.toggle_btn.setText("Начать запись")
        self.result_preview_active = False
        self.result_preview_text = ""
        self.unregister_cancel_hotkey()
        if show_overlay_result and text and self.cfg.overlay_enabled:
            self.result_preview_active = True
            self.result_preview_text = text
            self.overlay.show_result_text(text)
            self.register_cancel_hotkey()
            suffix = "поле ввода не найдено; текст показан под плашкой, сохранён в истории и скопирован в буфер"
        else:
            if self.cfg.overlay_enabled:
                QTimer.singleShot(1400, self.overlay.show_idle)
            if self.recording_started_in_own_window and text and not inserted:
                suffix = "текст показан во вкладке «Диктовка», сохранён в истории и скопирован в буфер"
            else:
                suffix = "текст вставлен" if inserted else "текст скопирован в буфер и сохранён в истории"
        self.status_label.setText(f"Готово: {suffix}.")

    def on_transcription_failed(self, detail: str, wav_path: Path) -> None:
        self._cleanup_wav(wav_path)
        if self.cancel_requested:
            log.info("Cancelled transcription failed after cancellation; suppressing user-facing error")
            self.update_recording_badge()
            self.unregister_cancel_hotkey()
            self.toggle_btn.setEnabled(True)
            self.toggle_btn.setText("Начать запись")
            self.status_label.setText("Расшифровка отменена. Фоновый результат проигнорирован.")
            return
        self.on_worker_failed(detail)

    def on_worker_failed(self, detail: str) -> None:
        self.download_btn.setEnabled(True)
        self.toggle_btn.setEnabled(True)
        self.toggle_btn.setText("Начать запись")
        self.update_recording_badge()
        self.unregister_cancel_hotkey()
        if self.cfg.overlay_enabled:
            self.overlay.show_error("Ошибка", seconds=5)
        log.error("Worker failed detail: %s", detail)
        QMessageBox.critical(self, "Ошибка", detail)
        self.status_label.setText("Произошла ошибка. Подробности записаны в папку логов.")

    def _cleanup_wav(self, wav_path: Path) -> None:
        if self.cfg.save_audio_debug:
            return
        try:
            wav_path.unlink(missing_ok=True)
        except Exception:
            pass

    def on_timer_tick(self) -> None:
        self.update_recording_badge()
        self.maybe_start_live_transcription()

    def update_recording_badge(self) -> None:
        if self.recorder.is_recording:
            self.record_blink = not self.record_blink
            color = "#ef4444" if self.record_blink else "#7f1d1d"
            self.record_badge.setStyleSheet(f"background: {color}; color: white; border-radius: 14px; padding: 8px 12px;")
            self.record_badge.setText(f"Запись {self.recorder.elapsed:0.1f} с")
            if self.cfg.overlay_enabled:
                self.overlay.show_recording(self.recorder.elapsed, live_enabled=self.cfg.live_transcription)
        else:
            if self.record_badge.text() != "Отменено":
                self.record_badge.setStyleSheet("background: #27272a; color: #f4f4f5; border-radius: 14px; padding: 8px 12px;")
            if not (self.transcribe_worker and self.transcribe_worker.isRunning()):
                if self.record_badge.text() != "Отменено":
                    self.record_badge.setText("Ожидание")

    def maybe_start_live_transcription(self) -> None:
        if not self.cfg.live_transcription or not self.recorder.is_recording or self.cancel_requested:
            return
        selected = self.cfg.selected_model
        if ALL_MODELS[selected].engine == "Parakeet":
            if not self.live_unavailable_notice_shown:
                self.live_unavailable_notice_shown = True
                self.status_label.setText("Для Parakeet live-режим временно отключён; после остановки будет финальная расшифровка.")
            return
        if self.live_worker and self.live_worker.isRunning():
            return
        now = time.perf_counter()
        interval = max(0.7, float(self.cfg.live_update_interval_seconds))
        if now - self.live_last_request_at < interval:
            return
        elapsed = self.recorder.elapsed
        first_request = self.live_processed_until_seconds <= 0.0
        min_window = 1.1 if first_request else max(0.9, interval)
        overlap = 0.8
        start = 0.0 if first_request else max(0.0, self.live_processed_until_seconds - overlap)
        if elapsed - start < min_window:
            return
        try:
            wav_path, duration = self.recorder.snapshot_segment_to_wav(start, elapsed, prefix="live")
        except Exception as exc:  # noqa: BLE001
            log.exception("Live snapshot failed")
            self.status_label.setText(f"Не удалось подготовить live-фрагмент: {exc}")
            return
        self.live_last_request_at = now
        cfg = AppConfig.load()
        self.live_worker = TranscribeWorker(self.models, cfg.selected_model, wav_path, duration, cfg, is_live=True)
        self.live_worker.finished_text.connect(lambda text, dur, path=wav_path, segment_end=elapsed: self.on_live_transcription_done(text, dur, path, segment_end))
        self.live_worker.failed.connect(lambda detail, path=wav_path: self.on_live_failed(detail, path))
        self.live_worker.start()

    def on_live_transcription_done(self, text: str, duration: float, wav_path: Path, segment_end: float) -> None:
        self._cleanup_wav(wav_path)
        if self.cancel_requested or not self.recorder.is_recording:
            return
        text = text.strip()
        if text:
            before = self.live_total_text
            self.live_total_text = merge_transcript_parts([self.live_total_text, text]).strip() if self.live_total_text else text
            delta = _word_delta(before, self.live_total_text)
            self.last_text.setPlainText(self.live_total_text)
            if self.recorder.is_recording and self.cfg.live_insert_confirmed_text and self.cfg.auto_paste and self.live_target_is_text_field:
                if delta:
                    inserted = copy_and_maybe_paste(delta, auto_paste=True, only_when_text_field_detected=self.cfg.paste_only_when_text_field_detected)
                    if inserted:
                        self.live_had_insertions = True
                        if self.cfg.overlay_enabled:
                            self.overlay.set_preview("")
                    elif self.cfg.overlay_enabled:
                        self.overlay.set_preview(self.live_total_text)
            elif self.cfg.overlay_enabled:
                self.overlay.set_preview(self.live_total_text)
        self.live_processed_until_seconds = max(self.live_processed_until_seconds, segment_end)

    def on_live_failed(self, detail: str, wav_path: Path) -> None:
        self._cleanup_wav(wav_path)
        log.error("Live transcription failed: %s", detail)
        self.status_label.setText("Live-расшифровка не удалась; финальная расшифровка всё равно будет выполнена.")
        # The final pass is started directly on stop; failed live chunks are ignored.

    def copy_overlay_result(self, text: str) -> None:
        text = text.strip()
        if text:
            pyperclip.copy(text)
        self.result_preview_active = False
        self.result_preview_text = ""
        if self.cfg.overlay_enabled:
            self.overlay.show_idle()
        self.unregister_cancel_hotkey()
        self.status_label.setText("Текст скопирован. Готово.")

    def on_overlay_position_changed(self, x: int, y: int) -> None:
        self.cfg.overlay_x = int(x)
        self.cfg.overlay_y = int(y)
        self.cfg.save()

    def refresh_history(self) -> None:
        self.history_list.clear()
        for item in self.history.recent(limit=100):
            preview = item.text.replace("\n", " ")[:72]
            if item.source == "file":
                source = f"Файл: {item.file_name or 'без имени'}"
            else:
                source = "Диктовка"
            label = f"{_format_history_date(item.created_at)} · {source} · {model_display_name(item.model_key)} · {preview}"
            list_item = QListWidgetItem(label)
            list_item.setData(Qt.UserRole, item)
            self.history_list.addItem(list_item)

    def show_history_item(self, current: QListWidgetItem | None, previous: QListWidgetItem | None) -> None:
        if not current:
            return
        item = current.data(Qt.UserRole)
        if isinstance(item, HistoryItem):
            self.history_text.setPlainText(item.text)

    def delete_history_item(self) -> None:
        current = self.history_list.currentItem()
        if not current:
            return
        item = current.data(Qt.UserRole)
        if isinstance(item, HistoryItem):
            self.history.delete(item.id)
            self.refresh_history()
            self.history_text.clear()

    def clear_history(self) -> None:
        result = QMessageBox.question(self, "Очистить историю", "Удалить все сохранённые расшифровки?")
        if result != QMessageBox.Yes:
            return
        self.history.clear()
        self.refresh_history()
        self.history_text.clear()

    def closeEvent(self, event: QCloseEvent) -> None:
        event.ignore()
        self.hide()
        self.tray.showMessage("Voice Input Local", "Приложение продолжает работать в трее.", QSystemTrayIcon.Information, 2500)
        self._sync_overlay_visibility()

    def really_quit(self) -> None:
        self.unregister_cancel_hotkey()
        if self.file_transcribe_worker and self.file_transcribe_worker.isRunning():
            self.file_transcribe_worker.cancel()
        self.hotkey.stop()
        self.overlay.hide_overlay()
        app = QApplication.instance()
        if app is not None:
            app.quit()


def run() -> int:
    setup_logging()
    app = QApplication(sys.argv)
    app.setApplicationName("Voice Input Local")
    icon = QIcon(str(app_icon_path()))
    if not icon.isNull():
        app.setWindowIcon(icon)
    app.setQuitOnLastWindowClosed(False)
    window = MainWindow()
    if "--minimized" not in sys.argv:
        window.show()
    return app.exec()
