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
from PySide6.QtGui import QAction, QCloseEvent, QIcon, QKeySequence, QStandardItem, QStandardItemModel, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
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
    QScrollArea,
    QSpinBox,
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
from .models import ALL_MODELS, DEFAULT_MODEL_KEY, DEFAULT_SUMMARY_MODEL_KEY, SUMMARY_MODELS, DownloadProgress, ModelManager, TRANSCRIPTION_MODELS, cloud_provider_of, is_cloud_model_key, merge_transcript_parts, model_display_name
from .overlay import RecordingOverlay
from .paths import app_icon_path, logs_dir, models_dir
from .updater import UpdateInfo, launch_update_file, normalize_repo
from .workers import CloudConnectionCheckWorker, DownloadWorker, FileProgress, FileTranscribeWorker, FileTranscriptBlock, MicrophoneAutodetectWorker, MicrophoneAutodetectResult, PreloadWorker, SummarizeWorker, TranscribeWorker, UpdateCheckWorker, UpdateDownloadWorker
try:
    from .summarizer import DEFAULT_SUMMARY_PROMPT
except ImportError:
    DEFAULT_SUMMARY_PROMPT = ""

log = get_logger("ui")

APP_STYLE = """
QMainWindow { background: #101114; color: #f4f4f5; }
QWidget { font-size: 13px; color: #f4f4f5; background: #101114; }
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


class NoScrollComboBox(QComboBox):
    """QComboBox that allows wheel-scroll ONLY while the dropdown popup is open.

    Prevents accidental value changes when scrolling the settings page (BUG-01).
    Wheel events are always forwarded to the nearest QScrollArea unless the
    dropdown list is currently visible. This means:
      - Scrolling over a closed combo → settings page scrolls (good)
      - Scrolling over a focused-but-closed combo → settings page scrolls (good)
      - Scrolling while the dropdown is open → combo value changes (expected)
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._popup_visible = False
        self.setFocusPolicy(Qt.StrongFocus)

    def showPopup(self) -> None:
        self._popup_visible = True
        super().showPopup()

    def hidePopup(self) -> None:
        self._popup_visible = False
        super().hidePopup()

    def wheelEvent(self, event) -> None:  # noqa: ANN001
        if self._popup_visible:
            super().wheelEvent(event)
        else:
            # Forward wheel to nearest QScrollArea so settings page scrolls
            ancestor = self.parent()
            while ancestor is not None:
                if isinstance(ancestor, QScrollArea):
                    ancestor.wheelEvent(event)
                    return
                ancestor = ancestor.parent()
            event.ignore()


class EditableClickToOpenComboBox(NoScrollComboBox):
    """Editable QComboBox, который открывает popup по клику в любое место поля
    (а не только по стрелке справа). Полезно для cloud-моделей в настройках,
    где пользователь обычно хочет выбрать из списка, но иногда — вписать
    кастомный id для нестандартных провайдеров.

    Редактирование текста остаётся доступным через клавиатуру / двойной клик.
    TASK-048.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setEditable(True)
        # Установим eventFilter на lineEdit, чтобы перехватывать клики по полю
        if self.lineEdit() is not None:
            self.lineEdit().installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:  # noqa: ANN001
        from PySide6.QtCore import QEvent
        if obj is self.lineEdit() and event.type() == QEvent.MouseButtonPress:
            # Открываем popup и съедаем событие — фокус всё равно перейдёт
            # в lineEdit, и пользователь сможет редактировать клавиатурой
            self.showPopup()
            return True
        return super().eventFilter(obj, event)


class NoScrollSpinBox(QSpinBox):
    """QSpinBox, который не меняет значение при скролле страницы (BUG-04).

    Колесо мыши работает только когда поле в фокусе (пользователь явно
    кликнул). Иначе wheel-событие пересылается в ближайший QScrollArea,
    чтобы прокручивалась вкладка настроек, а не значение SpinBox'a.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, event) -> None:  # noqa: ANN001
        if self.hasFocus():
            super().wheelEvent(event)
            return
        ancestor = self.parent()
        while ancestor is not None:
            if isinstance(ancestor, QScrollArea):
                ancestor.wheelEvent(event)
                return
            ancestor = ancestor.parent()
        event.ignore()


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
        # US-015, US-016: построить реестр cloud-моделей из сохранённых ключей.
        # Для UI-показа placeholder'ов работает и без ключей.
        try:
            self.models.refresh_cloud_models(self.cfg)
        except Exception as exc:  # noqa: BLE001
            log.warning("Initial refresh_cloud_models failed: %s", exc)
        self._cloud_check_worker = None  # ссылка на CloudConnectionCheckWorker (anti-GC)
        self.recorder = AudioRecorder(sample_rate=self.cfg.sample_rate, input_device_id=self.cfg.audio_input_device_id, meeting_compatibility=self.cfg.audio_meeting_compatibility)
        self.transcribe_worker: TranscribeWorker | None = None
        self.file_transcribe_worker: FileTranscribeWorker | None = None
        self.microphone_autodetect_worker: MicrophoneAutodetectWorker | None = None
        self.update_check_worker: UpdateCheckWorker | None = None
        self.update_download_worker: UpdateDownloadWorker | None = None
        self.pending_update_info: UpdateInfo | None = None
        self.summarize_worker: SummarizeWorker | None = None
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
        QTimer.singleShot(1500, self.start_initial_cloud_discover)  # TASK-045
        QTimer.singleShot(1800, lambda: self.check_for_updates(manual=False))
        if self.cfg.api_enabled:
            QTimer.singleShot(2500, self._start_api_server)

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
        # BUG-CL-02: длинные сообщения об ошибках облака (URL + причина) раздвигали
        # окно по ширине. WordWrap + SizePolicy(Ignored, Preferred) разрешают QLabel
        # ужиматься в ширину доступной строки вместо раздвигания layout.
        self.status_label.setWordWrap(True)
        from PySide6.QtWidgets import QSizePolicy
        self.status_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.status_label.setMinimumWidth(0)
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
        # TASK-047: иначе после клика по кнопке она остаётся в keyboard focus,
        # и нажатие Space (часть hotkey Ctrl+Space) активирует её через Qt
        # default-button behavior — диктовка стартует/стопится без Ctrl.
        # Глобальный hotkey через keyboard library продолжает работать.
        self.toggle_btn.setFocusPolicy(Qt.NoFocus)
        self.toggle_btn.setAutoDefault(False)
        self.toggle_btn.setDefault(False)
        self.toggle_btn.clicked.connect(self.toggle_recording)
        layout.addWidget(self.toggle_btn)

        self.last_text = QTextEdit()
        self.last_text.setPlaceholderText("Последняя расшифровка появится здесь.")
        layout.addWidget(self.last_text, 1)

        actions = QHBoxLayout()
        copy_btn = QPushButton("Скопировать текст")
        copy_btn.setFocusPolicy(Qt.NoFocus)
        copy_btn.setAutoDefault(False)
        copy_btn.clicked.connect(lambda: pyperclip.copy(self.last_text.toPlainText()))
        insert_btn = QPushButton("Вставить текст сейчас")
        insert_btn.setFocusPolicy(Qt.NoFocus)
        insert_btn.setAutoDefault(False)
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
        self.file_speaker_count_combo = NoScrollComboBox()
        self.file_speaker_count_combo.addItem("Авто", "auto")
        self.file_speaker_count_combo.addItem("2", "2")
        self.file_speaker_count_combo.addItem("3", "3")
        self.file_speaker_count_combo.addItem("4", "4")
        speaker_row.addWidget(QLabel("Количество говорящих:"))
        speaker_row.addWidget(self.file_speaker_count_combo)
        speaker_row.addStretch(1)
        self.file_summary_check = QCheckBox("Суммаризация (краткие итоги диалога после расшифровки)")
        self.file_summary_check.setToolTip("После расшифровки локальная LLM-модель создаст краткое резюме. Модель скачается автоматически при первом использовании (~2.5 ГБ).")
        options_box.addWidget(self.file_stable_timestamps_check)
        options_box.addWidget(self.file_diarization_check)
        options_box.addLayout(speaker_row)
        options_box.addWidget(self.file_summary_check)
        layout.addLayout(options_box)

        action_row = QHBoxLayout()
        self.file_transcribe_btn = QPushButton("Расшифровать файл")
        self.file_transcribe_btn.setObjectName("Primary")
        # TASK-047: убираем focus, чтобы Space не активировал кнопку
        self.file_transcribe_btn.setFocusPolicy(Qt.NoFocus)
        self.file_transcribe_btn.setAutoDefault(False)
        self.file_transcribe_btn.clicked.connect(self.start_file_transcription)
        self.file_cancel_btn = QPushButton("Отменить")
        self.file_cancel_btn.setObjectName("Danger")
        self.file_cancel_btn.setFocusPolicy(Qt.NoFocus)
        self.file_cancel_btn.setAutoDefault(False)
        self.file_cancel_btn.setEnabled(False)
        self.file_cancel_btn.clicked.connect(self.cancel_file_transcription)
        self.file_copy_btn = QPushButton("Скопировать результат")
        self.file_copy_btn.setFocusPolicy(Qt.NoFocus)
        self.file_copy_btn.setAutoDefault(False)
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

        self.file_summary_label = QLabel("Суммаризация:")
        self.file_summary_label.setObjectName("Subtitle")
        self.file_summary_label.setVisible(False)
        layout.addWidget(self.file_summary_label)
        self.file_summary_text = QTextEdit()
        self.file_summary_text.setReadOnly(True)
        self.file_summary_text.setPlaceholderText("Здесь появится краткое резюме расшифровки.")
        self.file_summary_text.setMaximumHeight(180)
        self.file_summary_text.setVisible(False)
        layout.addWidget(self.file_summary_text)
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
        # ── QScrollArea & dark theme ────────────────────────────────────
        # The dark background for this tab comes from the global rule:
        #   QWidget { background: #101114; }
        # in APP_STYLE.  This is the ONLY reliable way to ensure all
        # QScrollArea viewports, QTabWidget panes, and QFormLayout rows
        # get the dark background on Windows.
        #
        # Previous (broken) approaches — DO NOT use:
        #   - tab.setStyleSheet("background: transparent;")
        #     → cascades into ALL children, wiping QLineEdit/QComboBox
        #       dark backgrounds (#18181b) to transparent
        #   - scroll.setStyleSheet("QScrollArea > QWidget {…}")
        #     → Qt CSS does not support the ">" child combinator
        #   - scroll.viewport().setAutoFillBackground(False)
        #     → unreliable, viewport palette overrides this
        #   - QWidget#SettingsInner { background: transparent; }
        #     → doesn't help when QScrollArea viewport has its own palette
        #
        # The fix is at the source: APP_STYLE line
        #   QWidget { background: #101114; }
        # If you remove or break that rule, white background WILL return.
        # ────────────────────────────────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
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
        self.microphone_combo = NoScrollComboBox()
        self.refresh_microphones_btn = QPushButton("Обновить список")
        self.refresh_microphones_btn.clicked.connect(lambda: self.refresh_microphone_combo())
        if not autostart.is_supported():
            self.autostart_check.setEnabled(False)
            self.autostart_check.setToolTip("Автозагрузка доступна только в Windows.")

        self.language_combo = NoScrollComboBox()
        self.language_combo.addItem("Авто", "")
        for code in ["ru", "en", "kk", "de", "fr", "es", "it", "nl", "pt", "uk", "pl"]:
            self.language_combo.addItem(code, code)
        self.device_combo = NoScrollComboBox()
        for v in ["cpu", "cuda", "auto"]:
            self.device_combo.addItem(v, v)
        self.compute_combo = NoScrollComboBox()
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

        summary_separator = QLabel("Суммаризация")
        summary_separator.setObjectName("Title")
        summary_separator.setStyleSheet("font-size: 16px; margin-top: 12px;")
        form.addRow(summary_separator)
        self.summary_prompt_edit = QTextEdit()
        self.summary_prompt_edit.setPlaceholderText("Системный промпт для суммаризации. Оставьте пустым для промпта по умолчанию.")
        self.summary_prompt_edit.setMaximumHeight(120)
        self.summary_prompt_edit.setToolTip("Настройте промпт под свой тип звонков: продажи, поддержка, переговоры.")
        form.addRow("Промпт суммаризации", self.summary_prompt_edit)
        self.summary_prompt_reset_btn = QPushButton("По умолчанию")
        self.summary_prompt_reset_btn.setToolTip("Сбросить промпт суммаризации к встроенному значению.")
        self.summary_prompt_reset_btn.clicked.connect(self.reset_summary_prompt)
        form.addRow("", self.summary_prompt_reset_btn)

        # ── Cloud STT (US-015, US-016, US-032) ────────────────────────────
        cloud_separator = QLabel("Облачные модели")
        cloud_separator.setObjectName("Title")
        cloud_separator.setStyleSheet("font-size: 16px; margin-top: 12px;")
        form.addRow(cloud_separator)

        cloud_hint = QLabel(
            "Облачные модели обеспечивают более высокое качество распознавания, но требуют интернета "
            "и передают аудио на серверы провайдера. Если связь пропадёт — программа автоматически переключится "
            "на локальную модель из списка ниже."
        )
        cloud_hint.setWordWrap(True)
        cloud_hint.setObjectName("Subtitle")
        form.addRow("", cloud_hint)

        # OpenAI-compatible
        self.openai_stt_key_edit = QLineEdit()
        self.openai_stt_key_edit.setEchoMode(QLineEdit.Password)
        self.openai_stt_key_edit.setPlaceholderText("sk-…")
        self.openai_stt_key_edit.setToolTip("API-ключ OpenAI (или любого OpenAI-совместимого STT-провайдера).")
        form.addRow("OpenAI API Key", self.openai_stt_key_edit)

        self.openai_stt_base_url_edit = QLineEdit()
        self.openai_stt_base_url_edit.setPlaceholderText("https://api.openai.com/v1")
        self.openai_stt_base_url_edit.setToolTip("Base URL OpenAI-совместимого API. Для Groq: https://api.groq.com/openai/v1")
        form.addRow("OpenAI Base URL", self.openai_stt_base_url_edit)

        # TASK-048: editable + click-to-open поведение
        self.openai_stt_model_combo = EditableClickToOpenComboBox()
        self.openai_stt_model_combo.setToolTip(
            "Модель STT провайдера. Нажмите «Проверить соединение», чтобы обновить список из API."
        )
        form.addRow("OpenAI Model", self.openai_stt_model_combo)

        self.openai_check_btn = QPushButton("Проверить соединение и обновить список моделей")
        self.openai_check_btn.clicked.connect(lambda: self.check_cloud_connection("openai"))
        form.addRow("", self.openai_check_btn)

        # ElevenLabs
        self.elevenlabs_stt_key_edit = QLineEdit()
        self.elevenlabs_stt_key_edit.setEchoMode(QLineEdit.Password)
        self.elevenlabs_stt_key_edit.setPlaceholderText("…")
        self.elevenlabs_stt_key_edit.setToolTip("API-ключ ElevenLabs.")
        form.addRow("ElevenLabs API Key", self.elevenlabs_stt_key_edit)

        # TASK-048: editable + click-to-open поведение
        self.elevenlabs_stt_model_combo = EditableClickToOpenComboBox()
        self.elevenlabs_stt_model_combo.setToolTip("Модель ElevenLabs STT (по умолчанию scribe_v1).")
        form.addRow("ElevenLabs Model", self.elevenlabs_stt_model_combo)

        self.elevenlabs_check_btn = QPushButton("Проверить соединение и обновить список моделей")
        self.elevenlabs_check_btn.clicked.connect(lambda: self.check_cloud_connection("elevenlabs"))
        form.addRow("", self.elevenlabs_check_btn)

        # Параметры fallback и нарезки
        self.cloud_max_chunk_spin = NoScrollSpinBox()
        self.cloud_max_chunk_spin.setRange(30, 300)
        self.cloud_max_chunk_spin.setSuffix(" сек")
        self.cloud_max_chunk_spin.setToolTip(
            "Длинная диктовка нарезается на чанки этой длины и отправляется в облако параллельно (US-032)."
        )
        form.addRow("Длина чанка для облака", self.cloud_max_chunk_spin)

        self.cloud_fallback_combo = NoScrollComboBox()
        # Заполняется в _load_settings_into_ui — все локальные транскрипционные модели
        self.cloud_fallback_combo.setToolTip(
            "Локальная модель, которая используется при недоступности облака."
        )
        form.addRow("Fallback при сбое облака", self.cloud_fallback_combo)

        api_separator = QLabel("API-сервер")
        api_separator.setObjectName("Title")
        api_separator.setStyleSheet("font-size: 16px; margin-top: 12px;")
        form.addRow(api_separator)
        self.api_enabled_check = QCheckBox("Включить REST API (перезапуск требуется)")
        self.api_enabled_check.setToolTip("Запускает HTTP-сервер на localhost для приёма запросов на расшифровку от внешних приложений.")
        form.addRow("API", self.api_enabled_check)
        self.api_port_edit = QLineEdit()
        self.api_port_edit.setPlaceholderText("8672")
        self.api_port_edit.setToolTip("Порт для API-сервера. По умолчанию 8672.")
        form.addRow("Порт API", self.api_port_edit)
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        self.api_key_edit.setPlaceholderText("Оставьте пустым для открытого доступа")
        self.api_key_edit.setToolTip("Bearer-токен для авторизации. Если пусто, API доступен без авторизации.")
        form.addRow("API-ключ", self.api_key_edit)
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
        self.autodetect_mic_btn = QPushButton("Автонастройка микрофона")
        self.autodetect_mic_btn.setProperty("originalText", "Автонастройка микрофона")
        self.autodetect_mic_btn.clicked.connect(lambda: self.start_microphone_autodetect(manual=True))
        logs_btn = QPushButton("Открыть папку логов")
        logs_btn.clicked.connect(self.open_logs_folder)
        models_dir_btn = QPushButton("Открыть папку моделей")
        models_dir_btn.clicked.connect(self.open_models_folder)
        self.check_updates_btn = QPushButton("Проверить обновления")
        self.check_updates_btn.setProperty("originalText", "Проверить обновления")
        self.check_updates_btn.clicked.connect(lambda: self.check_for_updates(manual=True))
        buttons.addWidget(self.autodetect_mic_btn)
        buttons.addWidget(logs_btn)
        buttons.addWidget(models_dir_btn)
        buttons.addWidget(self.check_updates_btn)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        layout.addStretch(1)
        scroll.setWidget(tab)
        return scroll

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
        self.summarize_history_btn = QPushButton("Суммаризировать")
        self.summarize_history_btn.setToolTip("Сформировать краткое резюме из текста выбранной расшифровки.")
        self.summarize_history_btn.clicked.connect(self.summarize_history_item)
        delete_btn = QPushButton("Удалить выбранную")
        delete_btn.setObjectName("Danger")
        delete_btn.clicked.connect(self.delete_history_item)
        clear_btn = QPushButton("Очистить историю")
        clear_btn.setObjectName("Danger")
        clear_btn.clicked.connect(self.clear_history)
        right.addWidget(copy_btn)
        right.addWidget(self.summarize_history_btn)
        right.addWidget(delete_btn)
        right.addWidget(clear_btn)

        self.history_summary_label = QLabel("Суммаризация:")
        self.history_summary_label.setObjectName("Subtitle")
        self.history_summary_label.setVisible(False)
        right.addWidget(self.history_summary_label)
        self.history_summary_text = QTextEdit()
        self.history_summary_text.setReadOnly(True)
        self.history_summary_text.setPlaceholderText("Здесь появится резюме.")
        self.history_summary_text.setMaximumHeight(160)
        self.history_summary_text.setVisible(False)
        right.addWidget(self.history_summary_text)
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
        if hasattr(self, "file_summary_check"):
            self.file_summary_check.setChecked(self.cfg.summary_enabled)
        if hasattr(self, "summary_prompt_edit"):
            self.summary_prompt_edit.setPlainText(self.cfg.summary_system_prompt)
        if hasattr(self, "api_enabled_check"):
            self.api_enabled_check.setChecked(self.cfg.api_enabled)
        if hasattr(self, "api_port_edit"):
            self.api_port_edit.setText(str(self.cfg.api_port) if self.cfg.api_port != 8672 else "")
        if hasattr(self, "api_key_edit"):
            self.api_key_edit.setText(self.cfg.api_key)
        # Cloud STT (US-015, US-016, US-032)
        if hasattr(self, "openai_stt_key_edit"):
            self.openai_stt_key_edit.setText(self.cfg.openai_stt_api_key)
            self.openai_stt_base_url_edit.setText(self.cfg.openai_stt_base_url or "https://api.openai.com/v1")
            self._fill_cloud_model_combo(self.openai_stt_model_combo, "openai", self.cfg.openai_stt_model_id)
            self.elevenlabs_stt_key_edit.setText(self.cfg.elevenlabs_stt_api_key)
            self._fill_cloud_model_combo(self.elevenlabs_stt_model_combo, "elevenlabs", self.cfg.elevenlabs_stt_model_id)
            self.cloud_max_chunk_spin.setValue(max(30, min(300, int(self.cfg.cloud_max_chunk_seconds or 60))))
            self._fill_cloud_fallback_combo()

    def _fill_cloud_model_combo(self, combo: QComboBox, provider: str, current: str) -> None:
        """Заполняет combo моделей провайдера из реестра ModelManager + текущее значение."""
        combo.blockSignals(True)
        combo.clear()
        seen: set[str] = set()
        for key in self.models.cloud_model_keys():
            if cloud_provider_of(key) != provider:
                continue
            mid = key.split(":", 2)[2] if key.count(":") >= 2 else ""
            if mid and mid not in seen:
                combo.addItem(mid, mid)
                seen.add(mid)
        if current and current not in seen:
            combo.addItem(current, current)
        if current:
            idx = combo.findData(current)
            if idx >= 0:
                combo.setCurrentIndex(idx)
        combo.blockSignals(False)

    def _fill_cloud_fallback_combo(self) -> None:
        """Заполняет combo выбора fallback-модели — только установленные локальные
        транскрипционные модели (TASK-046). Список совпадает с группой
        «── Локальные ──» на вкладке «Диктовка»."""
        if not hasattr(self, "cloud_fallback_combo"):
            return
        self.cloud_fallback_combo.blockSignals(True)
        self.cloud_fallback_combo.clear()
        local_keys = [k for k in self.models.available_model_keys() if not is_cloud_model_key(k)]
        for key in local_keys:
            self.cloud_fallback_combo.addItem(model_display_name(key), key)
        # Выбрать текущий; если его нет в доступных — берём DEFAULT_MODEL_KEY
        current = self.cfg.cloud_fallback_model_key or DEFAULT_MODEL_KEY
        idx = self.cloud_fallback_combo.findData(current)
        if idx < 0:
            idx = self.cloud_fallback_combo.findData(DEFAULT_MODEL_KEY)
        if idx >= 0:
            self.cloud_fallback_combo.setCurrentIndex(idx)
        self.cloud_fallback_combo.blockSignals(False)

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
        self.file_summary_check.stateChanged.connect(self.save_file_options)
        self.api_enabled_check.stateChanged.connect(self.schedule_settings_autosave)
        self.api_port_edit.editingFinished.connect(self.schedule_settings_autosave)
        self.api_key_edit.editingFinished.connect(self.schedule_settings_autosave)
        # Cloud STT (US-015, US-016, US-032). Изменение ключа/URL/модели
        # триггерит autosave + перестроение реестра cloud-моделей.
        if hasattr(self, "openai_stt_key_edit"):
            self.openai_stt_key_edit.editingFinished.connect(self.on_cloud_settings_changed)
            self.openai_stt_base_url_edit.editingFinished.connect(self.on_cloud_settings_changed)
            self.openai_stt_model_combo.editTextChanged.connect(self.schedule_settings_autosave)
            self.openai_stt_model_combo.currentIndexChanged.connect(self.schedule_settings_autosave)
            self.elevenlabs_stt_key_edit.editingFinished.connect(self.on_cloud_settings_changed)
            self.elevenlabs_stt_model_combo.editTextChanged.connect(self.schedule_settings_autosave)
            self.elevenlabs_stt_model_combo.currentIndexChanged.connect(self.schedule_settings_autosave)
            self.cloud_max_chunk_spin.valueChanged.connect(self.schedule_settings_autosave)
            self.cloud_fallback_combo.currentIndexChanged.connect(self.schedule_settings_autosave)

    def save_file_options(self) -> None:
        if self._settings_loading:
            return
        self.cfg.file_stable_timestamps_enabled = self.file_stable_timestamps_check.isChecked()
        self.cfg.file_diarization_enabled = self.file_diarization_check.isChecked()
        self.cfg.file_speaker_count = str(self.file_speaker_count_combo.currentData() or "auto")
        if hasattr(self, "file_summary_check"):
            self.cfg.summary_enabled = self.file_summary_check.isChecked()
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

    def _populate_model_combo_with_groups(self, combo: QComboBox, target_key: str) -> None:
        """Заполнить combo моделей с группами «── Локальные ──» / «── Облачные ──».
        Используется и для model_combo (диктовка), и для file_model_combo (файлы).
        TASK-051 (US-017): общий построитель — cloud-модели доступны и для файлов.
        """
        from PySide6.QtGui import QBrush, QColor

        std_model = QStandardItemModel(combo)

        def add_header(text: str) -> None:
            item = QStandardItem(text)
            item.setFlags(Qt.NoItemFlags)  # disabled, не выбираемый
            item.setData(None, Qt.UserRole)
            item.setData(QIcon(), Qt.DecorationRole)
            std_model.appendRow(item)

        def add_model_row(key: str, *, disabled: bool = False, suffix: str = "") -> None:
            label = model_display_name(key) + suffix
            item = QStandardItem(label)
            item.setData(key, Qt.UserRole)
            if disabled:
                item.setFlags(Qt.ItemIsEnabled & ~Qt.ItemIsSelectable | Qt.NoItemFlags)
                item.setForeground(QBrush(QColor("#71717a")))
            std_model.appendRow(item)

        # ── Локальные ──
        add_header("── Локальные ──")
        local_keys = [k for k in self.models.available_model_keys() if not is_cloud_model_key(k)]
        for key in local_keys:
            add_model_row(key)

        # ── Облачные ──
        cloud_keys = self.models.cloud_model_keys()
        if cloud_keys:
            add_header("── Облачные ──")
            for key in cloud_keys:
                available = self.models.is_available(key)
                if available:
                    add_model_row(key)
                else:
                    add_model_row(key, disabled=True, suffix=" (не настроено)")

        combo.setModel(std_model)
        # Восстановить выбор. Поиск по UserRole.
        self._select_combo_by_userdata(combo, str(target_key))

    def refresh_available_models_combo(self, *, force_current: bool = False) -> None:
        """Перерисовать combo выбора модели.
        force_current=True — игнорировать combo.currentData() и принудительно
        выбрать cfg.selected_model. Нужно после cloud→локальная fallback
        (BUG-CL-01): иначе combo держит старую cloud-модель, потому что она
        тоже is_available (ключ задан), и побеждает над только что переключённой
        локальной моделью в cfg.

        TASK-051 (US-017): file_model_combo теперь тоже с группами Локальные/Облачные.
        """
        current = self.cfg.selected_model if self.models.is_available(self.cfg.selected_model) else DEFAULT_MODEL_KEY

        # file_model_combo (файлы) — теперь с группами и cloud-моделями (US-017).
        # Поле для расшифровки файлов хранится в cfg.file_selected_model отдельно от диктовки.
        fcombo = getattr(self, "file_model_combo", None)
        if fcombo is not None:
            file_current = getattr(self.cfg, "file_selected_model", None) or current
            if not self.models.is_available(file_current):
                file_current = current if self.models.is_available(current) else DEFAULT_MODEL_KEY
            previous = file_current if force_current else (fcombo.currentData() or file_current)
            fcombo.blockSignals(True)
            self._populate_model_combo_with_groups(
                fcombo,
                target_key=str(previous) if (previous and self.models.is_available(str(previous))) else file_current,
            )
            fcombo.blockSignals(False)

        # model_combo (диктовка) — с группами «Локальные» / «Облачные» (US-021).
        mcombo = getattr(self, "model_combo", None)
        if mcombo is not None:
            previous = current if force_current else (mcombo.currentData() or current)
            mcombo.blockSignals(True)
            target_key = previous if (previous and self.models.is_available(str(previous))) else current
            self._populate_model_combo_with_groups(mcombo, target_key=str(target_key))
            mcombo.blockSignals(False)

    @staticmethod
    def _select_combo_by_userdata(combo: QComboBox, value: str) -> None:
        """Найти и выделить элемент по UserRole (нужно для QStandardItemModel)."""
        for i in range(combo.count()):
            if combo.itemData(i) == value:
                combo.setCurrentIndex(i)
                return

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
            self.hotkey.start(self.cfg.hotkey)
            self.status_label.setText(f"Готово. Горячая клавиша: {self.cfg.hotkey}")
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

    def open_models_folder(self) -> None:
        path = models_dir()
        try:
            if os.name == "nt":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:  # noqa: BLE001
            QMessageBox.information(self, "Модели", f"Папка моделей: {path}\n\nНе удалось открыть автоматически: {exc}")

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
        # Summary models — set as active summarization model
        if self.models.is_summary_model(key):
            if not self.models.is_installed(key):
                QMessageBox.information(self, "Модели", "Эта модель ещё не загружена. Сначала загрузите её.")
                return
            self.cfg.selected_summary_model = key
            self.cfg.save()
            self.status_label.setText(f"Модель суммаризации: {model_display_name(key)}")
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

    # ────────────────────────────────────────────────────────────────────
    # US-019 (TASK-062..065): Матрица блокировок диктовка ↔ расшифровка файла
    # ────────────────────────────────────────────────────────────────────
    # | Диктовка     | Файл         | Поведение                         |
    # |--------------|--------------|-----------------------------------|
    # | локальная    | локальная    | ЗАПРЕТ — второй процесс блокирован |
    # | локальная    | облачная     | разрешено                          |
    # | облачная     | локальная    | разрешено                          |
    # | облачная     | облачная     | разрешено                          |
    #
    # Облачные операции не используют локальный микрофон/модель, поэтому
    # ресурсного конфликта нет. Локальный микрофон используется только
    # диктовкой (запись), локальная модель — обоими (CPU/GPU). Конфликт
    # ресурса возможен только при лок+лок.

    def is_dictation_busy(self) -> bool:
        """TASK-063: идёт ли сейчас запись или локальная/cloud расшифровка диктовки."""
        if self.recorder.is_recording:
            return True
        if self.transcribe_worker and self.transcribe_worker.isRunning():
            return True
        return False

    def is_file_busy(self) -> bool:
        """TASK-063: идёт ли сейчас расшифровка файла."""
        return self._file_job_running()

    def _dictation_model_key(self) -> str:
        """Активная или планируемая модель диктовки (берётся из cfg)."""
        return str(self.cfg.selected_model or DEFAULT_MODEL_KEY)

    def _file_model_key(self) -> str:
        """Активная или планируемая модель расшифровки файла."""
        # Если воркер уже запущен — берём model_key из воркера
        if self.file_transcribe_worker and self.file_transcribe_worker.isRunning():
            return str(getattr(self.file_transcribe_worker, "model_key", "") or self.cfg.file_selected_model or self.cfg.selected_model)
        # Иначе — то, что выбрано в combo / cfg
        fcombo = getattr(self, "file_model_combo", None)
        if fcombo is not None:
            data = fcombo.currentData()
            if data:
                return str(data)
        return str(self.cfg.file_selected_model or self.cfg.selected_model or DEFAULT_MODEL_KEY)

    def dictation_uses_local(self) -> bool:
        """TASK-063: True если активная/планируемая модель диктовки — локальная."""
        return not is_cloud_model_key(self._dictation_model_key())

    def file_uses_local(self) -> bool:
        """TASK-063: True если активная/планируемая модель файла — локальная."""
        return not is_cloud_model_key(self._file_model_key())

    def _can_start_dictation(self) -> tuple[bool, str]:
        """TASK-065: разрешено ли запустить диктовку прямо сейчас.

        Запрет ТОЛЬКО при попытке запустить локальную диктовку, когда уже
        идёт локальная расшифровка файла. Cloud-комбинации разрешены.
        Возвращает (ok, reason). reason — текст для статус-бара.
        """
        if self.is_file_busy() and self.file_uses_local() and self.dictation_uses_local():
            return False, (
                "Идёт локальная расшифровка файла. Чтобы диктовать параллельно — "
                "выберите облачную модель."
            )
        return True, ""

    def _can_start_file_transcribe(self) -> tuple[bool, str]:
        """TASK-065: разрешено ли запустить расшифровку файла прямо сейчас.

        Запрет ТОЛЬКО при попытке локальной расшифровки файла, когда уже
        идёт локальная диктовка. Cloud-комбинации разрешены.
        Возвращает (ok, reason). reason — текст для сообщения пользователю.
        """
        if self.is_dictation_busy() and self.dictation_uses_local() and self.file_uses_local():
            return False, (
                "Идёт локальная диктовка. Дождитесь её завершения или выберите "
                "облачную модель для расшифровки файла."
            )
        return True, ""

    def _confirm_cloud_oversize_file(self, file_size_mb: float, limit_mb: int, provider: str) -> bool:
        """TASK-053 (US-017): диалог при превышении лимита провайдера.

        Три кнопки: «Расшифровать через облако с автонарезкой» (default),
        «Переключусь на локальную модель» (закрывает диалог + подсветка
        file_model_combo на 1.5 сек), «Отмена».

        Возвращает True — если пользователь выбрал продолжить через cloud+автонарезка.
        Возвращает False — если выбрал переключиться на локальную или отменил.
        """
        provider_label = {"openai": "OpenAI", "elevenlabs": "ElevenLabs"}.get(provider, provider or "провайдера")
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Файл превышает лимит провайдера")
        dialog.setIcon(QMessageBox.Information)
        dialog.setText(
            f"Размер файла: {file_size_mb:.1f} МБ.\n"
            f"Лимит {provider_label} для прямой отправки: {limit_mb} МБ.\n\n"
            f"Как продолжить расшифровку?"
        )
        dialog.setInformativeText(
            "Файл будет автоматически разделён на части и отправлен в облако "
            "параллельно. Длинные записи (1–3 часа) обрабатываются за минуты — "
            "независимо от размера."
        )
        btn_cloud = dialog.addButton("Расшифровать через облако с автонарезкой", QMessageBox.AcceptRole)
        btn_local = dialog.addButton("Переключусь на локальную модель", QMessageBox.ActionRole)
        btn_cancel = dialog.addButton("Отмена", QMessageBox.RejectRole)
        dialog.setDefaultButton(btn_cloud)
        dialog.exec()
        clicked = dialog.clickedButton()
        if clicked is btn_cloud:
            return True
        if clicked is btn_local:
            # Подсветка file_model_combo на 1.5 сек — визуальная подсказка
            self._highlight_file_model_combo()
            return False
        # Cancel / закрытие крестиком
        return False

    def _cloud_internet_preflight(self, model_key: str) -> bool:
        """TASK-054 (US-017): pre-flight проверка интернета перед cloud-расшифровкой.

        Возвращает True — продолжать запуск воркера.
        Возвращает False — была применена авто-замена на локальную модель;
        пользователь должен сам нажать «Расшифровать» повторно.
        """
        from . import cloud_stt
        provider = cloud_provider_of(model_key)
        if provider == "openai":
            host = cloud_stt._host_from_url(self.cfg.openai_stt_base_url)
        elif provider == "elevenlabs":
            host = cloud_stt.ELEVENLABS_HOST
        else:
            return True
        try:
            ok = cloud_stt.is_internet_available(host)
        except Exception:  # noqa: BLE001
            ok = True  # неуверены — пусть воркер сам отработает с fallback
        if ok:
            return True
        # Сети нет — авто-fallback на локальную
        fallback_key = self.cfg.cloud_fallback_model_key or DEFAULT_MODEL_KEY
        if not self.models.is_available(fallback_key):
            fallback_key = DEFAULT_MODEL_KEY
        self.cfg.selected_model = fallback_key
        self.cfg.file_selected_model = fallback_key
        try:
            self.cfg.save()
        except Exception:  # noqa: BLE001
            pass
        self.refresh_available_models_combo(force_current=True)
        local_name = model_display_name(fallback_key)
        cloud_name = model_display_name(model_key)
        msg = (
            f"Нет соединения с интернетом. {cloud_name} недоступна. "
            f"Переключено на локальную: {local_name}. Запустите расшифровку повторно."
        )
        self.status_label.setText(msg)
        log.warning("Cloud file pre-flight: no internet (%s) → fallback to %s", host, fallback_key)
        try:
            tray = getattr(self, "tray", None)
            if tray is not None and tray.isVisible():
                tray.showMessage("Облако недоступно", msg, QSystemTrayIcon.Warning, 6000)
        except Exception:  # noqa: BLE001
            pass
        return False

    def _highlight_file_model_combo(self) -> None:
        """TASK-053 (US-017): кратковременная подсветка file_model_combo
        после выбора «Переключусь на локальную модель» — чтобы пользователь
        понял, где выбирать модель."""
        fcombo = getattr(self, "file_model_combo", None)
        if fcombo is None:
            return
        try:
            original_style = fcombo.styleSheet()
        except Exception:  # noqa: BLE001
            original_style = ""
        try:
            fcombo.setStyleSheet(original_style + " QComboBox { border: 2px solid #f59e0b; }")
            QTimer.singleShot(1500, lambda: fcombo.setStyleSheet(original_style))
            fcombo.setFocus()
            try:
                fcombo.showPopup()
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass

    def start_file_transcription(self) -> None:
        # US-019 (TASK-064/065): матричная проверка вместо глобальной блокировки.
        # Лок+лок → запрет; лок+cloud / cloud+лок / cloud+cloud → разрешено.
        if self._mic_autodetect_running():
            QMessageBox.information(self, "Файлы", "Идёт автонастройка микрофона. Дождитесь её завершения.")
            return
        if self._file_job_running():
            QMessageBox.information(self, "Файлы", "Файл уже расшифровывается. Дождитесь завершения или нажмите «Отменить».")
            return
        ok, reason = self._can_start_file_transcribe()
        if not ok:
            QMessageBox.information(self, "Файлы", reason)
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
        key = str(self.file_model_combo.currentData() or self.cfg.file_selected_model or self.cfg.selected_model)
        if not self.models.is_available(key):
            QMessageBox.information(self, "Модели", "Выбранная модель ещё не загружена. Сначала загрузите её во вкладке «Модели».")
            self.refresh_available_models_combo()
            return
        # TASK-051 (US-017): сохраняем выбор файловой модели отдельно от диктовки
        self.cfg.file_selected_model = key
        self.save_settings(auto=True)
        self.save_file_options()
        # TASK-053 (US-017): pre-flight проверка размера для cloud-моделей.
        # Если исходный файл превышает лимит провайдера — показываем диалог
        # с 3 кнопками: «cloud+автонарезка» (default), «переключусь на локальную»,
        # «отмена». На «переключусь» — подсвечиваем file_model_combo на 1.5с.
        if is_cloud_model_key(key):
            provider = cloud_provider_of(key)
            try:
                from .cloud_stt import provider_file_size_limit_mb as _limit_mb_fn
                limit_mb = _limit_mb_fn(provider)
            except Exception:  # noqa: BLE001
                limit_mb = None
            if limit_mb:
                try:
                    file_size_mb = path.stat().st_size / (1024.0 * 1024.0)
                except Exception:  # noqa: BLE001
                    file_size_mb = 0.0
                if file_size_mb > limit_mb:
                    if not self._confirm_cloud_oversize_file(file_size_mb, limit_mb, provider):
                        return
            # TASK-054 (US-017): pre-flight проверка интернета для cloud-моделей.
            # Делаем до конвертации файла в WAV — это экономит время если сети нет.
            # При недоступности — авто-fallback на cloud_fallback_model_key и return,
            # пользователь увидит уведомление и сам решит запускать ли расшифровку локально.
            if not self._cloud_internet_preflight(key):
                return
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
        # TASK-055 (US-017): cloud→локальная fallback при сбое cloud-расшифровки файла
        self.file_transcribe_worker.fallback_applied.connect(self.on_cloud_fallback_applied)
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
        # TASK-080 (US-019): не перезаписываем status_label, если идёт диктовка —
        # иначе её сообщения о записи/распознавании затираются прогрессом файла.
        if not self.is_dictation_busy() and not getattr(self, "result_preview_active", False):
            self.status_label.setText(progress.message)
        # TASK-080: overlay не должен перезаписываться прогрессом файла,
        # если в этот момент идёт диктовка (recording/processing) или
        # пользователю показывается развёрнутый результат (result_preview_active).
        # Overlay диктовки имеет приоритет — пользователь должен видеть свой
        # текст для копирования, а не «Файл · 42%».
        if self.cfg.overlay_enabled:
            if self.is_dictation_busy() or getattr(self, "result_preview_active", False):
                # Overlay сейчас обслуживает диктовку — НЕ трогаем его.
                # Прогресс файла виден на вкладке «Файлы» (file_progress + file_status_label).
                pass
            else:
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
        # TASK-084 (US-019): hotkey re-register СРАЗУ при клике cancel
        # (не дожидаясь окончания всех in-flight чанков, что может занять
        # до 30 сек). Keyboard listener мог потерять Win32-хук во время
        # длительной cloud-операции.
        try:
            self.register_hotkey(show_errors=False)
            log.info("Hotkey re-registered on cancel click (defensive)")
        except Exception as exc:  # noqa: BLE001
            log.exception("Hotkey re-register on cancel failed: %s", exc)

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
        # TASK-081 (US-019): defensive перерегистрация hotkey также после
        # успешного завершения длительной cloud-расшифровки.
        try:
            self.register_hotkey(show_errors=False)
        except Exception as exc:  # noqa: BLE001
            log.warning("Hotkey re-register after done failed: %s", exc)
        log.info("File transcription done: model=%s path=%s chars=%s", model_key, file_path, len(text))
        # SUM-01: Auto-summarize if checkbox is checked
        if text and self.file_summary_check.isChecked():
            self._start_file_summary(text)

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
        if self.cfg.overlay_enabled and not self.is_dictation_busy():
            self.overlay.show_cancelled(seconds=3)
        self._reset_file_transcription_ui()
        # TASK-081 (US-019): defensive перерегистрация hotkey.
        # После длительной cloud-расшифровки (особенно с отменой через
        # ThreadPoolExecutor.shutdown) keyboard listener иногда теряет
        # активный Win32-хук. register_hotkey() переустанавливает его
        # поверх старого — это безопасно (см. CLAUDE.md, раздел про hotkey).
        try:
            self.register_hotkey(show_errors=False)
            log.info("Hotkey re-registered after file transcription cancel (defensive)")
        except Exception as exc:  # noqa: BLE001
            log.exception("Hotkey re-register failed: %s", exc)
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
        if hasattr(self, "summary_prompt_edit"):
            self.cfg.summary_system_prompt = self.summary_prompt_edit.toPlainText().strip()
        if hasattr(self, "api_enabled_check"):
            self.cfg.api_enabled = self.api_enabled_check.isChecked()
        if hasattr(self, "api_port_edit") and self.api_port_edit.text().strip():
            try:
                self.cfg.api_port = int(self.api_port_edit.text().strip())
            except ValueError:
                pass
        if hasattr(self, "api_key_edit"):
            self.cfg.api_key = self.api_key_edit.text().strip()
        # Cloud STT (US-015, US-016, US-032)
        if hasattr(self, "openai_stt_key_edit"):
            old_openai_key = self.cfg.openai_stt_api_key
            old_openai_url = self.cfg.openai_stt_base_url
            old_eleven_key = self.cfg.elevenlabs_stt_api_key
            self.cfg.openai_stt_api_key = self.openai_stt_key_edit.text().strip()
            self.cfg.openai_stt_base_url = (self.openai_stt_base_url_edit.text().strip() or "https://api.openai.com/v1")
            self.cfg.openai_stt_model_id = str(self.openai_stt_model_combo.currentText() or "").strip()
            self.cfg.elevenlabs_stt_api_key = self.elevenlabs_stt_key_edit.text().strip()
            self.cfg.elevenlabs_stt_model_id = str(self.elevenlabs_stt_model_combo.currentText() or "").strip()
            self.cfg.cloud_max_chunk_seconds = int(self.cloud_max_chunk_spin.value())
            fb = str(self.cloud_fallback_combo.currentData() or DEFAULT_MODEL_KEY)
            self.cfg.cloud_fallback_model_key = fb
            # Если ключи/URL изменились — инвалидируем кэш discover и
            # перестраиваем реестр (без HTTP — только из cfg.*_stt_model_id).
            # После смены ключа cloud-модели в списке диктовки исчезают, пока
            # пользователь не нажмёт «Проверить соединение» (US-021 — не показываем
            # модели, не подтверждённые провайдером).
            if (
                old_openai_key != self.cfg.openai_stt_api_key
                or old_openai_url != self.cfg.openai_stt_base_url
                or old_eleven_key != self.cfg.elevenlabs_stt_api_key
            ):
                try:
                    from . import cloud_stt as _cs
                    _cs.invalidate_discover_cache()
                except Exception:  # noqa: BLE001
                    pass
                # При смене ключа сбрасываем сохранённую модель — иначе
                # старая запись cfg.openai_stt_model_id будет регистрировать
                # модель чужого провайдера в реестре.
                if old_openai_key != self.cfg.openai_stt_api_key:
                    self.cfg.openai_stt_model_id = ""
                if old_eleven_key != self.cfg.elevenlabs_stt_api_key:
                    self.cfg.elevenlabs_stt_model_id = ""
                try:
                    self.models.refresh_cloud_models(self.cfg)
                    self.refresh_available_models_combo()
                except Exception as exc:  # noqa: BLE001
                    log.warning("refresh_cloud_models after settings change failed: %s", exc)
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
        # US-019 (TASK-064): убрана глобальная блокировка при file_job_running.
        # Вместо неё — матричная проверка через _can_start_dictation().
        # Микрофон-автодетект и активный TranscribeWorker всё ещё блокируют
        # (это технические ограничения, не относящиеся к матрице US-019).
        # TASK-083 (US-019): расширенное логирование точки отказа.
        recorder_active = bool(self.recorder.is_recording)
        transcribe_running = bool(self.transcribe_worker and self.transcribe_worker.isRunning())
        file_running = bool(self._file_job_running())
        mic_auto = bool(self._mic_autodetect_running())
        log.info(
            "Toggle recording requested. recorder_active=%s transcribe_running=%s file_running=%s mic_auto=%s file_uses_local=%s dictation_uses_local=%s",
            recorder_active, transcribe_running, file_running, mic_auto,
            self.file_uses_local() if file_running else None,
            self.dictation_uses_local(),
        )
        if mic_auto:
            log.info("toggle_recording BLOCKED: mic autodetect running")
            self.status_label.setText("Идёт автонастройка микрофона. Диктовка временно недоступна.")
            return
        if transcribe_running:
            log.info("toggle_recording BLOCKED: transcribe_worker still running")
            return
        if recorder_active:
            log.info("toggle_recording: stopping current recording")
            self.stop_recording()
            return
        # TASK-065: матричная проверка перед стартом записи (лок+лок → запрет).
        ok, reason = self._can_start_dictation()
        if not ok:
            log.info("toggle_recording BLOCKED by _can_start_dictation: %s", reason)
            self.status_label.setText(reason)
            if self.cfg.overlay_enabled:
                # Кратко покажем плашку, чтобы пользователь увидел статус
                try:
                    self.overlay.show_processing()
                    QTimer.singleShot(1200, self.overlay.show_idle)
                except Exception:  # noqa: BLE001
                    pass
            return
        log.info("toggle_recording: starting recording")
        self.start_recording()

    def start_recording(self) -> None:
        # TASK-083 (US-019): логирование точки входа в start_recording
        log.info("start_recording entered. selected_model=%s overlay_enabled=%s", self.cfg.selected_model, self.cfg.overlay_enabled)
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
        # BUG-CL-03: для cloud-моделей пишем «отправляю в облако», а не «локально»
        cfg = AppConfig.load()
        if is_cloud_model_key(cfg.selected_model):
            self.status_label.setText(f"Отправляю в облако: {model_display_name(cfg.selected_model)}…")
        else:
            self.status_label.setText("Финальная расшифровка локально…")
        if self.cfg.overlay_enabled:
            self.overlay.show_processing()
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
        # US-015/US-016: запоминаем исходно выбранную модель, чтобы в сообщении
        # о fallback показать что именно облако упало.
        self._last_requested_model = cfg.selected_model
        self.transcribe_worker = TranscribeWorker(self.models, cfg.selected_model, wav_path, duration, cfg, is_live=False)
        self.transcribe_worker.finished_text.connect(lambda text, dur: self.on_transcription_done(text, dur, wav_path))
        self.transcribe_worker.fallback_applied.connect(self.on_cloud_fallback_applied)
        self.transcribe_worker.failed.connect(lambda detail, path=wav_path: self.on_transcription_failed(detail, path))
        self.transcribe_worker.start()

    def on_cloud_fallback_applied(self, fallback_key: str, reason: str) -> None:
        """US-015/US-016: облачная модель упала → переключились на локальную.
        Перечитываем cfg (transcribe_with_fallback его сохранил), обновляем UI.
        BUG-CL-01: force_current=True гарантирует, что combo переключится на
        локальную модель, а не останется на cloud (которая всё ещё is_available)."""
        try:
            self.cfg = AppConfig.load()
        except Exception:  # noqa: BLE001
            pass
        self.refresh_available_models_combo(force_current=True)
        local_name = model_display_name(fallback_key)
        # Имя облачной модели для сообщения — берём то, что было выбрано
        cloud_requested = getattr(self, "_last_requested_model", "")
        cloud_name = model_display_name(cloud_requested) if cloud_requested else "Облачная модель"
        msg = (
            f"{cloud_name} недоступна ({reason}). "
            f"Переключено на локальную: {local_name}. "
            f"Чтобы вернуться к облаку — выберите её в списке снова."
        )
        self.status_label.setText(msg)
        log.warning("Cloud fallback: %s → %s (%s)", cloud_requested, fallback_key, reason)
        # Уведомление через системный трей, если возможно
        try:
            self.tray.showMessage("Voice Input Local", msg, QSystemTrayIcon.Warning, 6000)
        except Exception:  # noqa: BLE001
            pass

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
            if item.summary:
                self.history_summary_label.setVisible(True)
                self.history_summary_text.setVisible(True)
                self.history_summary_text.setPlainText(item.summary)
            else:
                self.history_summary_label.setVisible(False)
                self.history_summary_text.setVisible(False)
                self.history_summary_text.clear()

    def delete_history_item(self) -> None:
        current = self.history_list.currentItem()
        if not current:
            return
        item = current.data(Qt.UserRole)
        if isinstance(item, HistoryItem):
            self.history.delete(item.id)
            self.refresh_history()
            self.history_text.clear()

    def _get_summary_model_path(self) -> str | None:
        """Resolve GGUF path for the selected summary model, or None."""
        key = self.cfg.selected_summary_model
        if not key or key not in SUMMARY_MODELS:
            # Try default
            key = DEFAULT_SUMMARY_MODEL_KEY
        if not self.models.is_installed(key):
            return None
        return str(self.models.summary_model_gguf_path(key))

    def _start_file_summary(self, text: str) -> None:
        """SUM-01: Start summarization after file transcription."""
        if self.summarize_worker and self.summarize_worker.isRunning():
            return
        model_path = self._get_summary_model_path()
        if not model_path:
            self.status_label.setText("Модель суммаризации не загружена. Загрузите её на вкладке Модели.")
            return
        self.file_summary_label.setVisible(True)
        self.file_summary_text.setVisible(True)
        self.file_summary_text.setPlainText("Суммаризирую…")
        self.status_label.setText("Формирую краткое резюме…")
        prompt = self.cfg.summary_system_prompt
        self.summarize_worker = SummarizeWorker(text, model_path, prompt)
        self.summarize_worker.finished_text.connect(self._on_file_summary_done)
        self.summarize_worker.failed.connect(self._on_summary_failed)
        self.summarize_worker.start()

    def _on_file_summary_done(self, summary: str) -> None:
        if summary.strip():
            self.file_summary_text.setPlainText(summary)
            self.status_label.setText("Расшифровка и суммаризация завершены.")
        else:
            self.file_summary_text.setPlainText("Суммаризация не дала результата.")
            self.status_label.setText("Файл расшифрован, но суммаризация пуста.")

    def summarize_history_item(self) -> None:
        """SUM-02: Summarize an existing history item without re-transcribing."""
        current = self.history_list.currentItem()
        if not current:
            QMessageBox.information(self, "Суммаризация", "Сначала выберите запись в списке.")
            return
        item = current.data(Qt.UserRole)
        if not isinstance(item, HistoryItem) or not item.text.strip():
            QMessageBox.information(self, "Суммаризация", "Выбранная запись не содержит текста.")
            return
        if self.summarize_worker and self.summarize_worker.isRunning():
            self.status_label.setText("Суммаризация уже выполняется, дождитесь завершения.")
            return
        model_path = self._get_summary_model_path()
        if not model_path:
            QMessageBox.information(self, "Суммаризация", "Модель суммаризации не загружена. Загрузите её на вкладке Модели.")
            return
        self.summarize_history_btn.setEnabled(False)
        self.status_label.setText("Суммаризирую…")
        prompt = self.cfg.summary_system_prompt
        self.summarize_worker = SummarizeWorker(item.text, model_path, prompt)
        self.summarize_worker.finished_text.connect(lambda text, item_id=item.id: self._on_history_summary_done(text, item_id))
        self.summarize_worker.failed.connect(self._on_summary_failed)
        self.summarize_worker.start()

    def _on_history_summary_done(self, summary: str, item_id: int) -> None:
        self.summarize_history_btn.setEnabled(True)
        if summary.strip():
            self.history.update_summary(item_id, summary)
            self.history_summary_label.setVisible(True)
            self.history_summary_text.setVisible(True)
            self.history_summary_text.setPlainText(summary)
            self.status_label.setText("Суммаризация завершена.")
        else:
            self.status_label.setText("Суммаризация не дала результата.")

    def _on_summary_failed(self, detail: str) -> None:
        self.summarize_history_btn.setEnabled(True)
        self.file_summary_label.setVisible(False)
        self.file_summary_text.setVisible(False)
        log.error("Summarization failed: %s", detail)
        self.status_label.setText("Суммаризация не удалась. Подробности в логах.")
        QMessageBox.warning(self, "Суммаризация", detail)

    def _start_api_server(self) -> None:
        """Start the REST API server if enabled (API-01..04)."""
        missing: list[str] = []
        for pkg in ("fastapi", "uvicorn", "multipart"):
            try:
                __import__(pkg)
            except ImportError:
                missing.append(pkg if pkg != "multipart" else "python-multipart")
        if missing:
            pkgs = ", ".join(missing)
            log.warning("API server dependencies missing: %s", pkgs)
            self.status_label.setText(f"API-сервер недоступен: pip install {pkgs}")
            return
        try:
            from .api_server import run_api_server
            run_api_server(self.models, self.cfg)
            self.status_label.setText(f"API-сервер запущен на порту {self.cfg.api_port}.")
            log.info("API server started on port %d", self.cfg.api_port)
        except Exception as exc:  # noqa: BLE001
            log.exception("API server failed to start")
            self.status_label.setText(f"API-сервер не запустился: {exc}")

    def reset_summary_prompt(self) -> None:
        """SUM-03: Reset summary prompt to default."""
        self.summary_prompt_edit.setPlainText("")
        self.cfg.summary_system_prompt = ""
        self.cfg.save()
        self.status_label.setText("Промпт суммаризации сброшен к значению по умолчанию.")

    # ── Cloud STT (US-015, US-016) ─────────────────────────────────────

    def on_cloud_settings_changed(self) -> None:
        """Сработало изменение ключа/URL cloud-провайдера → autosave.
        Сам пересчёт реестра cloud-моделей делается в save_settings()
        при детекте изменения ключа/URL."""
        self.schedule_settings_autosave()

    def start_initial_cloud_discover(self) -> None:
        """TASK-045: при старте программы — фоновая проверка соединения и
        автозагрузка списка моделей для каждого настроенного cloud-провайдера.
        При успехе тихо обновляет реестр; при ошибке (401/403/таймаут) — лог +
        статус-бар + трей-уведомление; если cfg.selected_model был cloud и
        discover упал — переключаемся на cfg.cloud_fallback_model_key."""
        try:
            import requests  # noqa: F401
        except ImportError:
            log.warning("Initial cloud discover skipped: requests not installed")
            return
        self._initial_cloud_check_workers: list = []
        if self.cfg.openai_stt_api_key:
            w = CloudConnectionCheckWorker(
                "openai",
                self.cfg.openai_stt_api_key,
                self.cfg.openai_stt_base_url or "https://api.openai.com/v1",
            )
            w.result.connect(lambda ok, msg, models, p="openai": self._on_initial_cloud_check_done(p, ok, msg, models))
            self._initial_cloud_check_workers.append(w)
            w.start()
            log.info("Initial cloud discover: started for openai")
        if self.cfg.elevenlabs_stt_api_key:
            w = CloudConnectionCheckWorker("elevenlabs", self.cfg.elevenlabs_stt_api_key, "")
            w.result.connect(lambda ok, msg, models, p="elevenlabs": self._on_initial_cloud_check_done(p, ok, msg, models))
            self._initial_cloud_check_workers.append(w)
            w.start()
            log.info("Initial cloud discover: started for elevenlabs")

    def _apply_cloud_models_to_settings_combo(self, provider: str, models: list) -> None:
        """TASK-049: общая логика обновления combo моделей в настройках после
        успешного discover (стартового или по кнопке). Сохраняет текущее
        значение если оно входит в новый список, иначе берёт первый id."""
        combo = self.openai_stt_model_combo if provider == "openai" else getattr(self, "elevenlabs_stt_model_combo", None)
        if combo is None:
            return
        current = combo.currentText().strip()
        combo.blockSignals(True)
        combo.clear()
        for mid in models:
            combo.addItem(mid, mid)
        if models:
            if current and current in models:
                combo.setCurrentIndex(combo.findData(current))
            else:
                combo.setCurrentIndex(0)
        elif current:
            # discover пустой → сохраняем пользовательский ввод
            combo.addItem(current, current)
            combo.setCurrentIndex(combo.count() - 1)
        combo.blockSignals(False)

    def _on_initial_cloud_check_done(self, provider: str, ok: bool, message: str, models: list) -> None:
        """Результат стартовой проверки cloud. Тихо при успехе; уведомление при сбое."""
        prefix = "OpenAI" if provider == "openai" else "ElevenLabs"
        log.info("Initial cloud discover [%s]: ok=%s msg=%s models=%d", provider, ok, message, len(models))
        if ok:
            try:
                self.models.set_cloud_models(provider, list(models))
                self.refresh_available_models_combo()
                self._apply_cloud_models_to_settings_combo(provider, list(models))
            except Exception as exc:  # noqa: BLE001
                log.warning("Initial set_cloud_models failed: %s", exc)
            return
        # Ошибка: уведомляем + проверяем не нужно ли fallback
        self.status_label.setText(f"{prefix}: {message} (стартовая проверка)")
        try:
            self.tray.showMessage("Voice Input Local", f"{prefix}: {message}", QSystemTrayIcon.Warning, 5000)
        except Exception:  # noqa: BLE001
            pass
        # Если в cfg выбрана cloud-модель этого провайдера — переключаемся на fallback
        if is_cloud_model_key(self.cfg.selected_model) and cloud_provider_of(self.cfg.selected_model) == provider:
            fallback_key = self.cfg.cloud_fallback_model_key or DEFAULT_MODEL_KEY
            if not self.models.is_available(fallback_key):
                fallback_key = DEFAULT_MODEL_KEY
            old = self.cfg.selected_model
            self.cfg.selected_model = fallback_key
            try:
                self.cfg.save()
            except Exception:  # noqa: BLE001
                pass
            log.warning("Initial cloud discover failed for selected model %s → switched to fallback %s", old, fallback_key)
            self.refresh_available_models_combo(force_current=True)

    def check_cloud_connection(self, provider: str) -> None:
        """Кнопка «Проверить соединение» — асинхронно проверяет ключ
        и обновляет список моделей провайдера в combo (US-015, US-016)."""
        if provider == "openai":
            api_key = self.openai_stt_key_edit.text().strip()
            base_url = (self.openai_stt_base_url_edit.text().strip() or "https://api.openai.com/v1")
            button = self.openai_check_btn
        elif provider == "elevenlabs":
            api_key = self.elevenlabs_stt_key_edit.text().strip()
            base_url = ""
            button = self.elevenlabs_check_btn
        else:
            return
        if not api_key:
            self.status_label.setText(f"Заполните API Key для {provider}.")
            return
        try:
            import requests  # noqa: F401
        except ImportError:
            self.status_label.setText(
                "Не установлена библиотека requests. Выполните: "
                ".venv\\Scripts\\pip install requests"
            )
            return
        self.save_settings(auto=True)
        button.setEnabled(False)
        button.setText("Проверяю…")
        self.status_label.setText(f"Проверка соединения с {provider}…")
        worker = CloudConnectionCheckWorker(provider, api_key, base_url)
        self._cloud_check_worker = worker
        worker.result.connect(lambda ok, msg, models, p=provider, b=button: self._on_cloud_check_done(p, b, ok, msg, models))
        worker.start()

    def _on_cloud_check_done(self, provider: str, button: QPushButton, ok: bool, message: str, models: list) -> None:
        button.setEnabled(True)
        button.setText("Проверить соединение и обновить список моделей")
        prefix = "OpenAI" if provider == "openai" else "ElevenLabs"
        log.info("Cloud verify result: provider=%s ok=%s msg=%s models=%d", provider, ok, message, len(models))
        if ok:
            combo = self.openai_stt_model_combo if provider == "openai" else self.elevenlabs_stt_model_combo
            current = combo.currentText().strip()
            combo.blockSignals(True)
            combo.clear()
            for mid in models:
                combo.addItem(mid, mid)
            if models:
                if current and current in models:
                    combo.setCurrentIndex(combo.findData(current))
                else:
                    combo.setCurrentIndex(0)
            elif current:
                combo.addItem(current, current)
                combo.setCurrentIndex(combo.count() - 1)
            combo.blockSignals(False)
            chosen = combo.currentText().strip()
            if provider == "openai":
                if chosen and (self.cfg.openai_stt_model_id != chosen) and (
                    not self.cfg.openai_stt_model_id or self.cfg.openai_stt_model_id not in (models or [])
                ):
                    log.info("Cloud verify: reset stale cfg.openai_stt_model_id %r → %r",
                             self.cfg.openai_stt_model_id, chosen)
                    self.cfg.openai_stt_model_id = chosen
                    self.cfg.save()
            elif provider == "elevenlabs":
                if chosen and (self.cfg.elevenlabs_stt_model_id != chosen) and (
                    not self.cfg.elevenlabs_stt_model_id or self.cfg.elevenlabs_stt_model_id not in (models or [])
                ):
                    log.info("Cloud verify: reset stale cfg.elevenlabs_stt_model_id %r → %r",
                             self.cfg.elevenlabs_stt_model_id, chosen)
                    self.cfg.elevenlabs_stt_model_id = chosen
                    self.cfg.save()
            try:
                self.models.set_cloud_models(provider, list(models))
                self.refresh_available_models_combo()
            except Exception as exc:  # noqa: BLE001
                log.warning("set_cloud_models after verify failed: %s", exc)
            if models:
                self.status_label.setText(f"{prefix}: {message}. Моделей найдено: {len(models)}.")
            else:
                hint = (
                    f"{prefix}: {message}. STT-моделей не найдено. "
                    "Введите id модели вручную в поле выше (combo редактируемое). "
                    "Подсказка: в app.log записан полный список моделей провайдера."
                )
                self.status_label.setText(hint)
        else:
            self.status_label.setText(f"{prefix}: {message}")

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
            self.file_transcribe_worker.wait(3000)
        if self.transcribe_worker and self.transcribe_worker.isRunning():
            self.transcribe_worker.wait(2000)
        if self.preload_worker and self.preload_worker.isRunning():
            self.preload_worker.wait(1500)
        try:
            self.hotkey.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.tray.hide()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.overlay.hide()
        except Exception:  # noqa: BLE001
            pass
        QApplication.quit()


def run() -> int:
    """Entry point: создаёт QApplication, главное окно и запускает event loop."""
    setup_logging()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    from PySide6.QtCore import QLockFile, QStandardPaths
    import tempfile as _tempfile
    _lock_dir = QStandardPaths.writableLocation(QStandardPaths.TempLocation) or _tempfile.gettempdir()
    _lock_path = str(Path(_lock_dir) / "VoiceInputLocal.lock")
    _lock = QLockFile(_lock_path)
    _lock.setStaleLockTime(30000)
    if not _lock.tryLock(100):
        log.warning("Another VoiceInputLocal instance is already running (lock=%s). Exiting.", _lock_path)
        try:
            QMessageBox.information(
                None,
                "Voice Input Local",
                "Приложение уже запущено. Откройте его из системного трея. "
                "Если значка нет — завершите процесс VoiceInputLocal.exe / python.exe в Диспетчере задач и запустите снова.",
            )
        except Exception:  # noqa: BLE001
            pass
        return 0
    app._voice_input_lock = _lock  # type: ignore[attr-defined]

    icon_path = app_icon_path()
    if icon_path:
        app.setWindowIcon(QIcon(str(icon_path)))
    window = MainWindow()
    window.show()
    return app.exec()
