from __future__ import annotations

import os
import json
import subprocess
import sys
import time
import math
from pathlib import Path
from string import punctuation

import pyperclip
from PySide6.QtCore import QObject, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QCloseEvent, QIcon, QKeySequence, QStandardItem, QStandardItemModel, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
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
    QSlider,
    QSpinBox,
    QSystemTrayIcon,
    QStyle,
    QStyleOptionSlider,
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
from .config import AppConfig, CloudConnection, CONNECTION_TYPE_OPENAI, CONNECTION_TYPE_ELEVENLABS, DEFAULT_OPENAI_INITIAL_PROMPT, DEFAULT_POSTPROCESS_SYSTEM_PROMPT, DEFAULT_SUMMARY_SYSTEM_PROMPT
from .history import HistoryItem, HistoryStore
from . import export as history_export
from .hotkeys import VK_ESCAPE, HotkeyService, normalize_hotkey
from .insert import copy_and_maybe_paste, focused_control_accepts_text, foreground_belongs_to_current_process, foreground_matches_window_handle, foreground_window_handle
from .key_watch import KeyStateWatcher
from .logger import get_logger, setup_logging
from .models import ALL_MODELS, DEFAULT_MODEL_KEY, DEFAULT_SUMMARY_MODEL_KEY, SUMMARY_MODELS, DownloadProgress, ModelManager, TRANSCRIPTION_MODELS, cloud_connection_id_of, cloud_provider_of, is_cloud_model_key, merge_transcript_parts, model_display_name
from .overlay import HotkeySafeComboBox, RecordingOverlay
from .session_lock import SessionLockWatcher
from .cloud_security_dialog import (
    confirm_external_switch,
    confirm_safe_switch,
    host_is_cloudru,
    normalize_endpoint,
)
from .paths import app_icon_path, logs_dir, models_dir
from .updater import UpdateInfo, launch_update_file, normalize_repo, updates_disabled_by_policy
from . import busy_marker, update_signal
from .workers import CloudConnectionCheckWorker, ConnectionVerifyWorker, LlmConnectionCheckWorker, PostProcessWorker, DownloadWorker, FileProgress, FileTranscribeWorker, FileTranscriptBlock, MicrophoneAutodetectWorker, MicrophoneAutodetectResult, PreloadWorker, SummarizeWorker, TranscribeWorker, UpdateCheckWorker, UpdateDownloadWorker
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


class NoScrollSlider(QSlider):
    """QSlider, который НЕ реагирует на прокрутку колесом мыши (BUG-05).

    По образцу NoScrollComboBox (BUG-01) / NoScrollSpinBox (BUG-04): курсор над
    ползунком не должен случайно менять значение при прокрутке страницы настроек.
    Значение меняется только двумя способами:
      * клик по дорожке — ручка сразу встаёт на позицию курсора (а не шагает на
        pageStep, как по умолчанию у QSlider);
      * захват ручки и её перетаскивание — штатным механизмом QSlider.

    Колесо мыши над ползунком пересылается в ближайший QScrollArea, чтобы
    прокручивалась вкладка настроек, а не значение ползунка.
    """

    def wheelEvent(self, event) -> None:  # noqa: ANN001
        # Значение колесом не меняем НИКОГДА (даже в фокусе) — только прокрутка вкладки.
        ancestor = self.parent()
        while ancestor is not None:
            if isinstance(ancestor, QScrollArea):
                ancestor.wheelEvent(event)
                return
            ancestor = ancestor.parent()
        event.ignore()

    def mousePressEvent(self, event) -> None:  # noqa: ANN001
        # Левый клик по дорожке — сразу на позицию курсора; клик по ручке — обычное
        # перетаскивание. После setValue ручка оказывается под курсором, поэтому
        # базовый mousePressEvent подхватывает её и тянет дальше без лишнего шага.
        if event.button() == Qt.LeftButton:
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            handle = self.style().subControlRect(
                QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, self
            )
            point = event.position().toPoint()
            if not handle.contains(point):
                self.setValue(self._value_from_pos(point))
        super().mousePressEvent(event)

    def _value_from_pos(self, point) -> int:  # noqa: ANN001
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(
            QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, self
        )
        handle = self.style().subControlRect(
            QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, self
        )
        if self.orientation() == Qt.Horizontal:
            slider_min = groove.x()
            slider_max = groove.right() - handle.width() + 1
            pos = point.x() - handle.width() // 2
        else:
            slider_min = groove.y()
            slider_max = groove.bottom() - handle.height() + 1
            pos = point.y() - handle.height() // 2
        return QStyle.sliderValueFromPosition(
            self.minimum(),
            self.maximum(),
            pos - slider_min,
            slider_max - slider_min,
            opt.upsideDown,
        )


class ConnectionDialog(QDialog):
    """US-037: диалог создания/редактирования облачного подключения.

    Поля: Название, Тип (OpenAI-совместимый / ElevenLabs), API URL, API Key,
    кнопка «Проверить соединение» (verify + discover в фоне). Возвращает
    CloudConnection через result_connection() при принятии.
    """

    def __init__(self, parent=None, connection=None, initial_safe=False) -> None:
        super().__init__(parent)
        self.setWindowTitle("Подключение" if connection is None else "Редактирование подключения")
        self.setModal(True)
        self.setMinimumWidth(420)
        self._conn = connection
        self._discovered = list(connection.discovered_models) if connection else []
        self._check_worker = None

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit(connection.name if connection else "")
        self.name_edit.setPlaceholderText("Например: OpenAI основной, Groq быстрый")
        form.addRow("Название", self.name_edit)

        self.type_combo = QComboBox()
        self.type_combo.addItem("OpenAI-совместимый API", CONNECTION_TYPE_OPENAI)
        self.type_combo.addItem("ElevenLabs API", CONNECTION_TYPE_ELEVENLABS)
        if connection is not None:
            _i = self.type_combo.findData(connection.type)
            if _i >= 0:
                self.type_combo.setCurrentIndex(_i)
        self.type_combo.currentIndexChanged.connect(self._on_type_changed)
        form.addRow("Тип", self.type_combo)

        self.url_edit = QLineEdit(connection.base_url if connection else "https://api.openai.com/v1")
        self.url_edit.setPlaceholderText("https://api.openai.com/v1")
        self.url_edit.textChanged.connect(self._update_safe_enabled)
        form.addRow("API URL", self.url_edit)

        self.key_edit = QLineEdit(connection.api_key if connection else "")
        self.key_edit.setEchoMode(QLineEdit.Password)
        self.key_edit.setPlaceholderText("sk-…")
        form.addRow("API Key", self.key_edit)

        # US-018 (per-connection): пометка безопасного внутреннего эндпоинта Cloud.ru.
        # Активна только если тип OpenAI-совместимый и Base URL содержит домен cloud.ru.
        self.safe_endpoint_check = QCheckBox("Внутренний безопасный эндпоинт Cloud.ru (данные не покидают защищённый контур)")
        self.safe_endpoint_check.setToolTip("Доступно только для Base URL с доменом cloud.ru. Отметьте для внутренней модели Cloud.ru — переключение на неё показывает зелёное «безопасно» вместо предупреждения о передаче данных наружу.")
        self.safe_endpoint_check.setChecked(bool(initial_safe))
        form.addRow("", self.safe_endpoint_check)
        layout.addLayout(form)

        self.test_btn = QPushButton("Проверить соединение")
        self.test_btn.clicked.connect(self._on_test)
        layout.addWidget(self.test_btn)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("Subtitle")
        layout.addWidget(self.status_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.Cancel).setText("Отмена")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._on_type_changed()

    def _on_type_changed(self) -> None:
        is_el = self.type_combo.currentData() == CONNECTION_TYPE_ELEVENLABS
        # ElevenLabs — единый эндпоинт, URL не нужен.
        self.url_edit.setEnabled(not is_el)
        if is_el:
            self.url_edit.clear()
        elif not self.url_edit.text().strip():
            self.url_edit.setText("https://api.openai.com/v1")
        self._update_safe_enabled()

    def _update_safe_enabled(self) -> None:
        is_openai = self.type_combo.currentData() == CONNECTION_TYPE_OPENAI
        ok = is_openai and host_is_cloudru(self.url_edit.text().strip())
        self.safe_endpoint_check.setEnabled(ok)
        if not ok and self.safe_endpoint_check.isChecked():
            self.safe_endpoint_check.setChecked(False)

    def is_marked_safe(self) -> bool:
        return self.safe_endpoint_check.isChecked() and self.safe_endpoint_check.isEnabled()

    def _on_test(self) -> None:
        key = self.key_edit.text().strip()
        if not key:
            self.status_label.setText("Введите API Key перед проверкой.")
            return
        ctype = self.type_combo.currentData()
        url = self.url_edit.text().strip() or "https://api.openai.com/v1"
        self.test_btn.setEnabled(False)
        self.status_label.setText("Проверяю соединение…")
        # US-037: подключение находит ВСЕ модели эндпоинта (фильтр по типу
        # функции применяется позже в настройках конкретной функции).
        w = ConnectionVerifyWorker(ctype, key, url if ctype == CONNECTION_TYPE_OPENAI else "")
        self._check_worker = w
        w.result.connect(self._on_test_result)
        w.start()

    def _on_test_result(self, ok: bool, message: str, models: list) -> None:
        self.test_btn.setEnabled(True)
        if ok:
            self._discovered = list(models or [])
            extra = f" Найдено моделей: {len(self._discovered)}." if self._discovered else ""
            self.status_label.setText(f"✓ Соединение успешно.{extra}")
        else:
            self.status_label.setText(f"✗ {message}")

    def _on_accept(self) -> None:
        if not self.name_edit.text().strip():
            self.status_label.setText("Введите название подключения.")
            return
        if not self.key_edit.text().strip():
            self.status_label.setText("Введите API Key.")
            return
        self.accept()

    def result_connection(self) -> "CloudConnection":
        ctype = self.type_combo.currentData()
        url = self.url_edit.text().strip() if ctype == CONNECTION_TYPE_OPENAI else ""
        key = self.key_edit.text().strip()
        name = self.name_edit.text().strip()
        if self._conn is not None:
            self._conn.name = name
            self._conn.type = ctype
            self._conn.base_url = url
            self._conn.api_key = key
            self._conn.discovered_models = list(self._discovered)
            return self._conn
        c = CloudConnection(name=name, type=ctype, base_url=url, api_key=key)
        c.discovered_models = list(self._discovered)
        return c


class HotkeySignal(QObject):
    triggered = Signal()


# US-026/US-066: сигналы Push-to-Talk. Колбэки приходят из нативного фильтра
# событий (WM_HOTKEY) и из таймера наблюдения за клавишами, поэтому доставляются
# сигналами с QueuedConnection: обработчик открывает диалоги и запускает запись,
# а делать это прямо внутри разбора нативного сообщения Windows нельзя.
class PttPressSignal(QObject):
    triggered = Signal()


class PttReleaseSignal(QObject):
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


class TermDialog(QDialog):
    """US-044: диалог создания/редактирования записи словаря терминов.

    Поля: Термин (обязательно), Частые искажения (через запятую), Контекст/
    описание, Исключения (когда НЕ заменять). По образцу ConnectionDialog.
    Возвращает запись через result_entry() при принятии.
    """

    def __init__(self, parent=None, entry=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Термин словаря" if entry is None else "Редактирование термина")
        self.setModal(True)
        self.setMinimumWidth(460)
        entry = entry or {}

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.term_edit = QLineEdit(str(entry.get("term", "") or ""))
        self.term_edit.setPlaceholderText("Например: Телеофис")
        form.addRow("Термин *", self.term_edit)

        self.distortions_edit = QLineEdit(str(entry.get("distortions", "") or ""))
        self.distortions_edit.setPlaceholderText("Через запятую: телефис, тело офис")
        form.addRow("Частые искажения", self.distortions_edit)

        self.context_edit = QTextEdit()
        self.context_edit.setPlainText(str(entry.get("context", "") or ""))
        self.context_edit.setPlaceholderText(
            "Что это и когда термин уместен. Например: название нашей компании, производитель оборудования."
        )
        self.context_edit.setFixedHeight(64)
        form.addRow("Контекст/описание", self.context_edit)

        self.exclusions_edit = QTextEdit()
        self.exclusions_edit.setPlainText(str(entry.get("exclusions", "") or ""))
        self.exclusions_edit.setPlaceholderText(
            "Когда НЕ подставлять термин. Например: когда речь об обычном телефонном офисе / колл-центре, а не о компании."
        )
        self.exclusions_edit.setFixedHeight(64)
        form.addRow("Исключения", self.exclusions_edit)

        layout.addLayout(form)

        hint = QLabel(
            "Обязательно только «Термин». Остальные поля помогают ИИ решать, где замена уместна, а где нет."
        )
        hint.setWordWrap(True)
        hint.setObjectName("Subtitle")
        layout.addWidget(hint)

        self.status_label = QLabel("")
        self.status_label.setObjectName("Subtitle")
        layout.addWidget(self.status_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.Cancel).setText("Отмена")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_accept(self) -> None:
        if not self.term_edit.text().strip():
            self.status_label.setText("Поле «Термин» обязательно.")
            return
        self.accept()

    def result_entry(self) -> dict:
        return {
            "term": self.term_edit.text().strip(),
            "distortions": self.distortions_edit.text().strip(),
            "context": self.context_edit.toPlainText().strip(),
            "exclusions": self.exclusions_edit.toPlainText().strip(),
        }


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        # US-045: версия в заголовке окна → видна в панели задач и Alt-Tab.
        self.setWindowTitle(f"Voice Input Local v{__version__}")
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
        self._llm_check_worker = None  # ссылка на LlmConnectionCheckWorker (anti-GC, US-034)
        self._postprocess_worker = None  # ссылка на PostProcessWorker (anti-GC, US-034)
        self._dictation_used_cloud = False  # US-034: последняя диктовка шла через облачный STT
        # US-018: сессионное (в памяти) подавление уведомлений о cloud-моделях.
        # Ключ набора: "<provider>|<normalized_endpoint>". Сбрасывается в
        # save_settings при смене ключа/URL провайдера (см. правило AC US-018).
        self._cloud_warned_session: set[str] = set()
        self._cloud_safe_confirmed_session: set[str] = set()
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
        # EPIC-13: QueuedConnection обязателен. WM_HOTKEY разбирается ВНУТРИ
        # нативного фильтра событий Qt, а обработчик открывает модальные окна и
        # запускает запись — вложенный event loop прямо в разборе сообщения ОС
        # недопустим. Сигнал переносит выполнение на следующий проход цикла.
        self.hotkey_signal.triggered.connect(self.toggle_recording, Qt.QueuedConnection)
        # US-026: сигналы Push-to-Talk (старт по нажатию, стоп по отпусканию).
        self.ptt_press_signal = PttPressSignal()
        self.ptt_press_signal.triggered.connect(self.on_hotkey_press, Qt.QueuedConnection)
        self.ptt_release_signal = PttReleaseSignal()
        self.ptt_release_signal.triggered.connect(self.on_hotkey_release, Qt.QueuedConnection)
        self.cancel_signal = CancelSignal()
        self.cancel_signal.triggered.connect(self.cancel_current_action, Qt.QueuedConnection)
        self.hotkey = HotkeyService(
            lambda: self.hotkey_signal.triggered.emit(),
            on_press=lambda: self.ptt_press_signal.triggered.emit(),
            on_release=lambda: self.ptt_release_signal.triggered.emit(),
        )
        # US-065: отмена по Escape — точечное наблюдение за ОДНОЙ клавишей и
        # только на время операции. Регистрировать Escape системной горячей
        # клавишей нельзя: Windows отдала бы её нам одним, и в активной
        # программе Escape перестал бы закрывать диалоги и подсказки.
        self.escape_watcher = KeyStateWatcher(self)
        # US-069: блокировка экрана останавливает диктовку. Состояние сессии
        # спрашивается у Windows только пока идёт запись — как и состояние
        # клавиш. Расшифровки файла это не касается: она идёт часами и с
        # присутствием пользователя не связана.
        self.session_lock_watcher = SessionLockWatcher(self)
        self._dictation_stopped_by_lock = False
        self._hotkey_error_message = ""
        self.record_blink = False
        self.overlay = RecordingOverlay()
        self.overlay.restore_position(self.cfg.overlay_x, self.cfg.overlay_y)
        self.overlay.copy_requested.connect(self.copy_overlay_result)
        self.overlay.position_changed.connect(self.on_overlay_position_changed)
        # US-019: автоматический выбор облачной модели через overlay-пикер.
        self.overlay.model_selected.connect(self.on_overlay_model_chosen)
        self.overlay.settings_requested.connect(self.on_overlay_settings_requested)
        self.overlay.picker_requested.connect(self.on_overlay_picker_requested)
        self.overlay.picker_cancelled.connect(self.on_overlay_picker_cancelled)
        # Двойной клик по плашке использует тот же переключатель, что основная
        # кнопка и глобальная горячая клавиша.
        self.overlay.toggle_recording_requested.connect(self.toggle_recording)
        self._audio_level_timer = QTimer(self)
        self._audio_level_timer.setInterval(50)
        self._audio_level_timer.timeout.connect(self._update_overlay_audio_level)
        self._overlay_picker_context = "quick"
        self._last_file_overlay_text = "Файл…"
        self.model_status_overrides: dict[str, str] = {}
        self.downloading_keys: set[str] = set()
        self.download_progress_frames = ["◌", "◔", "◑", "◕"]
        self.download_progress_tick = 0
        self.result_preview_active = False
        self.result_preview_text = ""

        self.recording_started_in_own_window = False
        # US-067: окно, активное на момент начала записи. Используется в режиме
        # «вставлять в окно, активное на момент начала записи».
        self.recording_target_hwnd: int | None = None
        self._settings_loading = True
        self._settings_save_timer = QTimer(self)
        self._settings_save_timer.setSingleShot(True)
        self._settings_save_timer.timeout.connect(self.auto_save_settings)

        # US-048: маркер занятости для безопасного централизованного обновления.
        # Пока идёт активная работа — периодически обновляем heartbeat; при
        # простое снимаем. Источник истины — is_dictation_busy()/is_file_busy()
        # (+суммаризация). Установщик читает маркер и откладывает обновление,
        # если heartbeat свежий, чтобы не прервать работу пользователя.
        # US-048: снять устаревший маркер, оставшийся после аварийного
        # завершения (при старте единственного экземпляра работа не идёт).
        busy_marker.clear()
        # US-058: снять устаревший маркер запроса закрытия для обновления
        # (мог остаться после аварийного завершения; single-instance lock
        # гарантирует, что при старте активной работы нет).
        update_signal.clear_update_close()
        update_signal.clear_update_decline()  # US-059: устаревший маркер отказа
        self._busy_marker_timer = QTimer(self)
        self._busy_marker_timer.setInterval(3000)
        self._busy_marker_timer.timeout.connect(self._update_busy_marker)
        self._busy_marker_timer.start()
        # US-057: флаг открытого диалога решения о централизованном обновлении.
        self._update_decision_open = False
        # US-058: флаг «идёт тихое закрытие ради обновления при простое».
        self._idle_update_closing = False

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
        if not self.register_hotkey(show_errors=False):
            # TASK-334 (US-064): сохранённая комбинация не регистрируется —
            # сообщаем заметно, а не строкой в статус-баре. С задержкой, чтобы
            # окно успело появиться и сообщение не всплыло раньше него.
            QTimer.singleShot(1200, self._notify_hotkey_registration_failed)
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

        # US-045: название + актуальная версия на одной строке. Единый QLabel
        # с rich-text: версия — инлайн-span, поэтому сидит на той же базовой
        # линии, что и название (QHBoxLayout+AlignBottom давал сдвиг вниз).
        # Версия из единого источника __version__ (не хардкод) → обновляется
        # автоматически при бампе. Видна на всех вкладках (шапка над QTabWidget).
        title = QLabel(
            "Voice Input Local"
            f'&nbsp;&nbsp;<span style="font-size:14px; font-weight:400; color:#a1a1aa;">v{__version__}</span>'
        )
        title.setObjectName("Title")
        title.setTextFormat(Qt.RichText)
        title.setToolTip(f"Версия установленной программы: v{__version__}")
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
        self.tabs.addTab(self._dictionary_tab(), "Словарь")  # US-044
        self.tabs.addTab(self._api_tab(), "API-Сервер")
        self.tabs.addTab(self._history_tab(), "История")
        root.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

    def _dictation_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        row = QHBoxLayout()
        self.model_combo = HotkeySafeComboBox()  # US-019: Space не открывает список (часть хоткея)
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
        # Системная регистрация комбинации при этом продолжает работать.
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
        # TASK-061 (US-017): inline-подсказка о способе диаризации для cloud-моделей.
        # Для OpenAI — локальный пост-процесс на CPU, для ElevenLabs — нативно.
        self.file_diarization_hint = QLabel("")
        self.file_diarization_hint.setObjectName("Subtitle")
        self.file_diarization_hint.setWordWrap(True)
        self.file_diarization_hint.setVisible(False)
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
        options_box.addWidget(self.file_diarization_hint)
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
        self.file_result_text.setMinimumHeight(90)
        layout.addWidget(self.file_result_text, 2)

        self.file_summary_label = QLabel("Суммаризация:")
        self.file_summary_label.setObjectName("Subtitle")
        self.file_summary_label.setVisible(False)
        layout.addWidget(self.file_summary_label)
        self.file_summary_text = QTextEdit()
        self.file_summary_text.setReadOnly(True)
        self.file_summary_text.setPlaceholderText("Здесь появится краткое резюме расшифровки.")
        self.file_summary_text.setMinimumHeight(60)
        self.file_summary_text.setMaximumHeight(220)
        self.file_summary_text.setVisible(False)
        layout.addWidget(self.file_summary_text, 1)

        # US-037: вкладку «Файлы» оборачиваем в прокрутку — появление панели
        # резюме НЕ меняет размер окна, содержимое просто прокручивается вниз.
        tab.setMinimumHeight(tab.sizeHint().height())
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(tab)
        return scroll

    def _models_tab(self) -> QWidget:
        # US-020: вкладка «Модели» делится на подвкладки «Локальные» и
        # «Облачные» — каждая занимает всё пространство страницы.
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(0, 0, 0, 0)
        self.models_subtabs = QTabWidget()
        self.models_subtabs.addTab(self._local_models_subtab(), "Локальные модели")
        self.models_subtabs.addTab(self._cloud_models_subtab(), "Облачные модели")
        outer.addWidget(self.models_subtabs)
        self._refresh_connections_table()
        return tab

    def _local_models_subtab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)
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

    def _cloud_models_subtab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)

        cloud_hint = QLabel(
            "Подключения к облачным провайдерам (OpenAI-совместимый API, ElevenLabs). "
            "Создайте подключение один раз — оно используется в диктовке, постобработке и суммаризации."
        )
        cloud_hint.setWordWrap(True)
        cloud_hint.setObjectName("Subtitle")
        layout.addWidget(cloud_hint)

        conn_label = QLabel("Подключения")
        conn_label.setObjectName("Title")
        conn_label.setStyleSheet("font-size: 15px; margin-top: 4px;")
        layout.addWidget(conn_label)

        self.connections_table = QTableWidget(0, 3)
        self.connections_table.setHorizontalHeaderLabels(["Название", "Тип", "Статус"])
        self.connections_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.connections_table.setSelectionMode(QTableWidget.SingleSelection)
        self.connections_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.connections_table.verticalHeader().setVisible(False)
        _conn_hh = self.connections_table.horizontalHeader()
        _conn_hh.setStretchLastSection(False)
        _conn_hh.setSectionResizeMode(0, QHeaderView.Stretch)
        _conn_hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        _conn_hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.connections_table.doubleClicked.connect(lambda *_: self._on_edit_connection())
        self.connections_table.itemSelectionChanged.connect(self._update_connection_buttons)
        self.connections_table.setMinimumHeight(150)
        self.connections_table.setMaximumHeight(240)
        layout.addWidget(self.connections_table)

        self.connections_empty_hint = QLabel(
            "Подключений нет. Нажмите «Добавить подключение», чтобы настроить облачного провайдера."
        )
        self.connections_empty_hint.setWordWrap(True)
        self.connections_empty_hint.setObjectName("Subtitle")
        layout.addWidget(self.connections_empty_hint)

        crow = QHBoxLayout()
        self.add_connection_btn = QPushButton("Добавить подключение")
        self.edit_connection_btn = QPushButton("Редактировать")
        self.delete_connection_btn = QPushButton("Удалить")
        self.delete_connection_btn.setObjectName("Danger")
        self.add_connection_btn.clicked.connect(self._on_add_connection)
        self.edit_connection_btn.clicked.connect(self._on_edit_connection)
        self.delete_connection_btn.clicked.connect(self._on_delete_connection)
        crow.addWidget(self.add_connection_btn)
        crow.addWidget(self.edit_connection_btn)
        crow.addWidget(self.delete_connection_btn)
        crow.addStretch(1)
        layout.addLayout(crow)

        # ── Диктовка (облако): Initial Prompt, чанк, fallback — US-035/US-037 ──
        dict_sep = QLabel("Настройки облачной расшифровки")
        dict_sep.setObjectName("Title")
        dict_sep.setStyleSheet("font-size: 15px; margin-top: 14px;")
        layout.addWidget(dict_sep)

        dict_note = QLabel("Облачную модель диктовки выбирайте на вкладке «Диктовка» (реквизиты берутся из подключения). Здесь — параметры облачной диктовки.")
        dict_note.setWordWrap(True)
        dict_note.setObjectName("Subtitle")
        layout.addWidget(dict_note)

        dict_group = QWidget()
        dgl = QFormLayout(dict_group)
        dgl.setContentsMargins(0, 0, 0, 0)

        self.openai_initial_prompt_edit = QTextEdit()
        self.openai_initial_prompt_edit.setPlaceholderText("Пример стиля пунктуации/форматирования. Оставьте пустым, чтобы не передавать prompt.")
        self.openai_initial_prompt_edit.setMinimumHeight(70)
        self.openai_initial_prompt_edit.setMaximumHeight(110)
        self.openai_initial_prompt_edit.setToolTip("Не команда модели, а пример стиля. Влияет на пунктуацию/форматирование, не меняет содержание. ~50 слов (лимит API 224 токена). Только диктовка через OpenAI-совместимый API; ElevenLabs не поддерживает.")
        dgl.addRow("Initial Prompt (Whisper/OpenAI)", self.openai_initial_prompt_edit)

        self.openai_prompt_reset_btn = QPushButton("Сбросить к дефолту")
        self.openai_prompt_reset_btn.setToolTip("Восстановить дефолтный пример стиля пунктуации.")
        self.openai_prompt_reset_btn.clicked.connect(self.on_reset_openai_initial_prompt)
        dgl.addRow("", self.openai_prompt_reset_btn)

        # EPIC-10/US-039: вырезание тишины перед отправкой в облако (диктовка)
        self.cloud_trim_silence_check = QCheckBox("Вырезать тишину перед отправкой в облако")
        self.cloud_trim_silence_check.setToolTip("Локальный VAD (Silero) убирает тишину и паузы ДО отправки — это устраняет галлюцинации облачных моделей на тишине. Только диктовка через облако; файлы и локальные модели не затрагиваются.")
        _trim_note = QLabel("Перед отправкой диктовки в облако тишина и паузы вырезаются локально (Silero VAD). Убирает «фантомные» фразы, которые облачный Whisper выдаёт на тишине. Если речь не найдена — запрос в облако не отправляется, плашка покажет «Речь не найдена».")
        _trim_note.setWordWrap(True)
        _trim_note.setObjectName("Subtitle")
        _trim_cell = QWidget()
        _tv = QVBoxLayout(_trim_cell)
        _tv.setContentsMargins(0, 0, 0, 0)
        _tv.setSpacing(2)
        _tv.addWidget(self.cloud_trim_silence_check)
        _tv.addWidget(_trim_note)
        dgl.addRow("Тишина (облако)", _trim_cell)

        # EPIC-10/US-039: непрерывный ползунок агрессивности (0..100) вместо трёх пунктов
        # BUG-05: NoScrollSlider — без реакции на колесо; клик по дорожке — сразу на значение
        self.cloud_trim_aggr_slider = NoScrollSlider(Qt.Horizontal)
        self.cloud_trim_aggr_slider.setRange(0, 100)
        self.cloud_trim_aggr_slider.setSingleStep(5)
        self.cloud_trim_aggr_slider.setPageStep(10)
        self.cloud_trim_aggr_slider.setTickPosition(QSlider.TicksBelow)
        self.cloud_trim_aggr_slider.setTickInterval(10)
        self.cloud_trim_aggr_slider.setToolTip("Насколько агрессивно вырезать тишину. Левее — бережно к тихой речи (режем меньше пауз); правее — агрессивнее режем паузы, но растёт риск срезать очень тихую речь. 50 — сбалансированное значение.")
        self.cloud_trim_aggr_value = QLabel("50")
        self.cloud_trim_aggr_value.setMinimumWidth(28)
        self.cloud_trim_aggr_slider.valueChanged.connect(
            lambda v: self.cloud_trim_aggr_value.setText(str(int(v)))
        )
        _aggr_lo = QLabel("Бережно"); _aggr_lo.setObjectName("Subtitle")
        _aggr_hi = QLabel("Агрессивно"); _aggr_hi.setObjectName("Subtitle")
        _aggr_row = QHBoxLayout()
        _aggr_row.setContentsMargins(0, 0, 0, 0)
        _aggr_row.setSpacing(6)
        _aggr_row.addWidget(_aggr_lo)
        _aggr_row.addWidget(self.cloud_trim_aggr_slider, 1)
        _aggr_row.addWidget(_aggr_hi)
        _aggr_row.addWidget(self.cloud_trim_aggr_value)
        _aggr_cell = QWidget()
        _aggr_cell.setLayout(_aggr_row)
        dgl.addRow("Агрессивность вырезания", _aggr_cell)

        self.cloud_max_chunk_spin = NoScrollSpinBox()
        self.cloud_max_chunk_spin.setRange(30, 300)
        self.cloud_max_chunk_spin.setSuffix(" сек")
        self.cloud_max_chunk_spin.setToolTip("Длинная диктовка нарезается на чанки этой длины и отправляется в облако параллельно (US-032).")
        _chunk_note = QLabel("Длинная запись разбивается на части указанной длины и отправляется в облако параллельно — это ускоряет и стабилизирует расшифровку длинных диктовок и файлов.")
        _chunk_note.setWordWrap(True)
        _chunk_note.setObjectName("Subtitle")
        _chunk_cell = QWidget()
        _cv = QVBoxLayout(_chunk_cell)
        _cv.setContentsMargins(0, 0, 0, 0)
        _cv.setSpacing(2)
        _cv.addWidget(self.cloud_max_chunk_spin)
        _cv.addWidget(_chunk_note)
        dgl.addRow("Длина чанка для облака", _chunk_cell)

        self.cloud_fallback_combo = NoScrollComboBox()
        self.cloud_fallback_combo.setToolTip("Локальная модель, которая используется при недоступности облака.")
        _fb_note = QLabel("Если облачная модель недоступна (нет интернета или сбой API), расшифровка автоматически выполнится этой локальной моделью.")
        _fb_note.setWordWrap(True)
        _fb_note.setObjectName("Subtitle")
        _fb_cell = QWidget()
        _fv = QVBoxLayout(_fb_cell)
        _fv.setContentsMargins(0, 0, 0, 0)
        _fv.setSpacing(2)
        _fv.addWidget(self.cloud_fallback_combo)
        _fv.addWidget(_fb_note)
        dgl.addRow("Fallback при сбое облака", _fb_cell)

        layout.addWidget(dict_group)

        # ── Улучшение расшифровки (постобработка облачной LLM) — US-020/US-034 ──
        pp_sep = QLabel("Улучшение расшифровки")
        pp_sep.setObjectName("Title")
        pp_sep.setStyleSheet("font-size: 15px; margin-top: 14px;")
        layout.addWidget(pp_sep)

        self.postprocess_enabled_check = QCheckBox("Включить постобработку расшифровки облачной LLM")
        self.postprocess_enabled_check.setToolTip("Работает только при использовании облачных STT-моделей. При локальной расшифровке постобработка не выполняется.")
        self.postprocess_enabled_check.toggled.connect(self._on_postprocess_enabled_toggled)
        layout.addWidget(self.postprocess_enabled_check)

        pp_note = QLabel("Сырой текст расшифровки (через облачный STT) прогоняется через облачную LLM: пунктуация, грамматика, формулировки. Локальная расшифровка не затрагивается.")
        pp_note.setWordWrap(True)
        pp_note.setObjectName("Subtitle")
        layout.addWidget(pp_note)

        self.postprocess_group = QWidget()
        pg = QFormLayout(self.postprocess_group)
        pg.setContentsMargins(0, 0, 0, 0)

        self.postprocess_conn_combo = NoScrollComboBox()
        self.postprocess_conn_combo.setToolTip("Облачное подключение (OpenAI-совместимое) для постобработки. Создайте его в блоке «Подключения» выше.")
        self.postprocess_conn_combo.currentIndexChanged.connect(self._on_postprocess_conn_changed)
        pg.addRow("Подключение", self.postprocess_conn_combo)

        # Скрытые поля совместимости: реквизиты берутся из выбранного подключения.
        self.postprocess_base_url_edit = QLineEdit()
        self.postprocess_base_url_edit.setVisible(False)
        self.postprocess_key_edit = QLineEdit()
        self.postprocess_key_edit.setEchoMode(QLineEdit.Password)
        self.postprocess_key_edit.setVisible(False)

        self.postprocess_model_combo = EditableClickToOpenComboBox()
        self.postprocess_model_combo.setToolTip("Модель LLM (text→text) для постобработки. Список отфильтрован по подключению.")
        pg.addRow("Модель", self.postprocess_model_combo)

        self.postprocess_check_btn = QPushButton("Проверить соединение и обновить список моделей")
        self.postprocess_check_btn.clicked.connect(self.check_llm_connection)
        pg.addRow("", self.postprocess_check_btn)

        self.postprocess_prompt_edit = QTextEdit()
        self.postprocess_prompt_edit.setMinimumHeight(180)
        self.postprocess_prompt_edit.setMaximumHeight(340)
        self.postprocess_prompt_edit.setToolTip("Инструкция для LLM: только форматирование (пунктуация, грамматика), без изменения смысла.")
        pg.addRow("Системный промпт", self.postprocess_prompt_edit)

        self.postprocess_prompt_reset_btn = QPushButton("Сбросить к дефолту")
        self.postprocess_prompt_reset_btn.setToolTip("Восстановить дефолтный системный промпт постобработки.")
        self.postprocess_prompt_reset_btn.clicked.connect(self.on_reset_postprocess_prompt)
        pg.addRow("", self.postprocess_prompt_reset_btn)

        self.postprocess_reasoning_check = QCheckBox("Рассуждение модели (Reasoning)")
        self.postprocess_reasoning_check.setToolTip("Рассуждение замедляет ответ; включайте только если качество важнее скорости.")
        self.postprocess_reasoning_check.toggled.connect(self._on_postprocess_reasoning_toggled)
        pg.addRow("Reasoning", self.postprocess_reasoning_check)

        self.postprocess_effort_row = QWidget()
        er = QHBoxLayout(self.postprocess_effort_row)
        er.setContentsMargins(0, 0, 0, 0)
        er.addWidget(QLabel("Уровень рассуждения:"))
        self.postprocess_reasoning_effort_combo = NoScrollComboBox()
        for _lvl, _name in (("low", "Низкий (быстро)"), ("medium", "Средний"), ("high", "Высокий (медленно)")):
            self.postprocess_reasoning_effort_combo.addItem(_name, _lvl)
        self.postprocess_reasoning_effort_combo.setToolTip("low — быстрее, high — качественнее, но медленнее.")
        er.addWidget(self.postprocess_reasoning_effort_combo)
        er.addStretch(1)
        pg.addRow("", self.postprocess_effort_row)

        self.postprocess_warn_label = QLabel("Заполните параметры подключения.")
        self.postprocess_warn_label.setObjectName("Subtitle")
        self.postprocess_warn_label.setStyleSheet("color: #f59e0b;")
        self.postprocess_warn_label.setVisible(False)
        pg.addRow("", self.postprocess_warn_label)

        layout.addWidget(self.postprocess_group)

        # ── Суммаризация (облако) — US-036/US-037 ──
        sm_sep = QLabel("Суммаризация")
        sm_sep.setObjectName("Title")
        sm_sep.setStyleSheet("font-size: 15px; margin-top: 14px;")
        layout.addWidget(sm_sep)

        sm_note = QLabel("Подключение и модель для облачной суммаризации. Способ суммаризации (локально или облако) выбирается в «Настройках»; промпт и Reasoning — ниже.")
        sm_note.setWordWrap(True)
        sm_note.setObjectName("Subtitle")
        layout.addWidget(sm_note)

        sm_group = QWidget()
        smg = QFormLayout(sm_group)
        smg.setContentsMargins(0, 0, 0, 0)

        self.summary_conn_combo = NoScrollComboBox()
        self.summary_conn_combo.setToolTip("Облачное подключение (OpenAI-совместимое) для суммаризации. Создайте его в блоке «Подключения» выше.")
        self.summary_conn_combo.currentIndexChanged.connect(self._on_summary_conn_changed)
        smg.addRow("Подключение", self.summary_conn_combo)

        # Скрытые поля совместимости: реквизиты берутся из выбранного подключения.
        self.summary_base_url_edit = QLineEdit()
        self.summary_base_url_edit.setVisible(False)
        self.summary_key_edit = QLineEdit()
        self.summary_key_edit.setEchoMode(QLineEdit.Password)
        self.summary_key_edit.setVisible(False)

        self.summary_model_combo = EditableClickToOpenComboBox()
        self.summary_model_combo.setToolTip("Модель LLM (text→text) для суммаризации. Список отфильтрован по подключению.")
        smg.addRow("Модель", self.summary_model_combo)

        self.summary_check_btn = QPushButton("Проверить соединение и обновить список моделей")
        self.summary_check_btn.clicked.connect(self.check_summary_connection)
        smg.addRow("", self.summary_check_btn)

        self.summary_prompt_edit = QTextEdit()
        self.summary_prompt_edit.setPlaceholderText("Системный промпт суммаризации (используется и для локального, и для облачного режима).")
        self.summary_prompt_edit.setMinimumHeight(180)
        self.summary_prompt_edit.setMaximumHeight(340)
        self.summary_prompt_edit.setToolTip("Роль, задача, ограничения и формат ответа. Кнопка «По умолчанию» вернёт встроенное значение.")
        smg.addRow("Системный промпт", self.summary_prompt_edit)

        self.summary_prompt_reset_btn = QPushButton("По умолчанию")
        self.summary_prompt_reset_btn.setToolTip("Сбросить промпт суммаризации к встроенному значению.")
        self.summary_prompt_reset_btn.clicked.connect(self.reset_summary_prompt)
        smg.addRow("", self.summary_prompt_reset_btn)

        self.summary_reasoning_check = QCheckBox("Рассуждение модели (Reasoning)")
        self.summary_reasoning_check.setToolTip("Модель размышляет перед ответом: качественнее, но медленнее. Для облачных моделей передаётся уровень рассуждения; для локальной Qwen3 включается режим thinking.")
        self.summary_reasoning_check.toggled.connect(self._on_summary_reasoning_toggled)
        smg.addRow("Reasoning", self.summary_reasoning_check)

        self.summary_effort_row = QWidget()
        ser = QHBoxLayout(self.summary_effort_row)
        ser.setContentsMargins(0, 0, 0, 0)
        ser.addWidget(QLabel("Уровень рассуждения:"))
        self.summary_reasoning_effort_combo = NoScrollComboBox()
        for _lvl, _name in (("low", "Низкий (быстро)"), ("medium", "Средний"), ("high", "Высокий (медленно)")):
            self.summary_reasoning_effort_combo.addItem(_name, _lvl)
        self.summary_reasoning_effort_combo.setToolTip("Применяется к облачным моделям. low — быстрее, high — качественнее.")
        ser.addWidget(self.summary_reasoning_effort_combo)
        ser.addStretch(1)
        smg.addRow("", self.summary_effort_row)

        layout.addWidget(sm_group)
        layout.addStretch(1)
        self._refresh_connection_pickers()
        scroll.setWidget(tab)
        return scroll

    # ── US-037: выбор подключения для функций (постобработка и др.) ──────
    def _fill_llm_model_combo(self, combo, conn, current: str = "") -> None:
        """Заполнить combo моделями подключения, отфильтрованными до text→text."""
        models = []
        if conn is not None:
            try:
                from .cloud_llm import _is_text_io_model
                models = [m for m in (conn.discovered_models or []) if _is_text_io_model(m)]
            except Exception:  # noqa: BLE001
                models = list(conn.discovered_models or [])
        combo.blockSignals(True)
        combo.clear()
        for m in models:
            combo.addItem(m)
        if current:
            combo.setCurrentText(current)
        combo.blockSignals(False)

    def _sync_postprocess_conn(self, autosave: bool) -> None:
        if not hasattr(self, "postprocess_conn_combo"):
            return
        conn = self.cfg.connection_by_id(self.postprocess_conn_combo.currentData() or "")
        if conn is not None:
            self.postprocess_base_url_edit.setText(conn.base_url or "https://api.openai.com/v1")
            self.postprocess_key_edit.setText(conn.api_key or "")
            self._fill_llm_model_combo(self.postprocess_model_combo, conn, getattr(self.cfg, "postprocess_model_id", "") or "")
        if hasattr(self, "postprocess_warn_label"):
            need = bool(getattr(self, "postprocess_enabled_check", None) and self.postprocess_enabled_check.isChecked())
            self.postprocess_warn_label.setVisible(need and conn is None)
        if autosave:
            self.schedule_settings_autosave()

    def _on_postprocess_conn_changed(self) -> None:
        self._sync_postprocess_conn(autosave=True)

    def _sync_summary_conn(self, autosave: bool) -> None:
        if not hasattr(self, "summary_conn_combo"):
            return
        conn = self.cfg.connection_by_id(self.summary_conn_combo.currentData() or "")
        if conn is not None:
            self.summary_base_url_edit.setText(conn.base_url or "https://api.openai.com/v1")
            self.summary_key_edit.setText(conn.api_key or "")
            self._fill_llm_model_combo(self.summary_model_combo, conn, getattr(self.cfg, "summary_model_id", "") or "")
        if autosave:
            self.schedule_settings_autosave()

    def _on_summary_conn_changed(self) -> None:
        self._sync_summary_conn(autosave=True)

    def _refresh_connection_pickers(self) -> None:
        """US-037: наполнить выпадающие списки «Подключение» в функциях."""
        if hasattr(self, "postprocess_conn_combo"):
            combo = self.postprocess_conn_combo
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("— выберите подключение —", "")
            for c in (getattr(self.cfg, "cloud_connections", None) or []):
                if c.type == "openai":
                    combo.addItem(c.name or c.id, c.id)
            cur = getattr(self.cfg, "postprocess_connection_id", "") or ""
            idx = combo.findData(cur)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.blockSignals(False)
            self._sync_postprocess_conn(autosave=False)
        if hasattr(self, "summary_conn_combo"):
            combo = self.summary_conn_combo
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("— выберите подключение —", "")
            for c in (getattr(self.cfg, "cloud_connections", None) or []):
                if c.type == "openai":
                    combo.addItem(c.name or c.id, c.id)
            cur = getattr(self.cfg, "summary_connection_id", "") or ""
            idx = combo.findData(cur)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.blockSignals(False)
            self._sync_summary_conn(autosave=False)

    # ── US-037: управление облачными подключениями ──────────────────────
    def _refresh_connections_table(self) -> None:
        tbl = getattr(self, "connections_table", None)
        if tbl is None:
            return
        conns = list(getattr(self.cfg, "cloud_connections", None) or [])
        type_label = {"openai": "OpenAI-совм.", "elevenlabs": "ElevenLabs"}
        tbl.setRowCount(len(conns))
        for r, c in enumerate(conns):
            tbl.setItem(r, 0, QTableWidgetItem(c.name or "(без названия)"))
            tbl.setItem(r, 1, QTableWidgetItem(type_label.get(c.type, c.type)))
            status = "Ключ задан" if c.api_key else "Нет ключа"
            if c.discovered_models:
                status += f" · моделей: {len(c.discovered_models)}"
            tbl.setItem(r, 2, QTableWidgetItem(status))
        if hasattr(self, "connections_empty_hint"):
            self.connections_empty_hint.setVisible(not conns)
        self._update_connection_buttons()

    def _update_connection_buttons(self) -> None:
        has_sel = getattr(self, "connections_table", None) is not None and self.connections_table.currentRow() >= 0
        if hasattr(self, "edit_connection_btn"):
            self.edit_connection_btn.setEnabled(has_sel)
            self.delete_connection_btn.setEnabled(has_sel)

    def _selected_connection(self):
        conns = list(getattr(self.cfg, "cloud_connections", None) or [])
        r = self.connections_table.currentRow()
        if 0 <= r < len(conns):
            return conns[r]
        return None

    def _on_add_connection(self) -> None:
        dlg = ConnectionDialog(self)
        if dlg.exec():
            conn = dlg.result_connection()
            self.cfg.cloud_connections.append(conn)
            self._apply_connection_safe(conn, dlg.is_marked_safe())
            self._after_connections_changed()

    def _on_edit_connection(self) -> None:
        conn = self._selected_connection()
        if conn is None:
            return
        dlg = ConnectionDialog(self, connection=conn, initial_safe=self._endpoint_marked_safe(conn.base_url))
        if dlg.exec():
            dlg.result_connection()  # мутирует conn на месте
            self._apply_connection_safe(conn, dlg.is_marked_safe())
            self._after_connections_changed()

    def _apply_connection_safe(self, conn, safe: bool) -> None:
        """US-018 (per-connection): добавить/убрать base_url подключения в списке
        безопасных эндпоинтов Cloud.ru и сбросить сессионные подтверждения для него."""
        lst = list(getattr(self.cfg, "cloud_internal_safe_endpoints", []))
        norm = normalize_endpoint(conn.base_url)
        present = norm in {normalize_endpoint(x) for x in lst}
        if safe and conn.type == "openai" and host_is_cloudru(conn.base_url):
            if norm and not present:
                lst.append((conn.base_url or "").strip())
        else:
            lst = [x for x in lst if normalize_endpoint(x) != norm]
        self.cfg.cloud_internal_safe_endpoints = lst
        if norm:
            self._cloud_warned_session = {k for k in self._cloud_warned_session if not k.endswith("|" + norm)}
            self._cloud_safe_confirmed_session = {k for k in self._cloud_safe_confirmed_session if not k.endswith("|" + norm)}

    def _on_delete_connection(self) -> None:
        conn = self._selected_connection()
        if conn is None:
            return
        used = []
        if getattr(self.cfg, "postprocess_connection_id", "") == conn.id:
            used.append("постобработка")
        if getattr(self.cfg, "summary_connection_id", "") == conn.id:
            used.append("суммаризация")
        if is_cloud_model_key(self.cfg.selected_model or "") and cloud_connection_id_of(self.cfg.selected_model) == conn.id:
            used.append("диктовка")
        msg = f"Удалить подключение «{conn.name or conn.id}»?"
        if used:
            msg += "\n\nОно используется в: " + ", ".join(used) + ". После удаления выберите другое подключение в соответствующей функции."
        if QMessageBox.question(self, "Удаление подключения", msg) != QMessageBox.Yes:
            return
        self.cfg.cloud_connections = [c for c in self.cfg.cloud_connections if c.id != conn.id]
        self._after_connections_changed()

    def _after_connections_changed(self) -> None:
        try:
            self.cfg.save()
            self.models.refresh_cloud_models(self.cfg)
            self.refresh_available_models_combo()
        except Exception as exc:  # noqa: BLE001
            log.warning("after connections changed failed: %s", exc)
        self._refresh_connections_table()
        if hasattr(self, "_refresh_connection_pickers"):
            try:
                self._refresh_connection_pickers()
            except Exception as exc:  # noqa: BLE001
                log.warning("refresh connection pickers failed: %s", exc)

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
        # US-047: пометка при централизованном отключении обновлений машинной политикой.
        self.updates_policy_label = QLabel("Обновлениями управляет системный администратор. Встроенная проверка обновлений отключена.")
        self.updates_policy_label.setObjectName("Subtitle")
        self.updates_policy_label.setWordWrap(True)
        self.updates_policy_label.setVisible(False)
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
        self.hotkey_hint_label = QLabel("Кликните поле и нажмите новую комбинацию. Нужен хотя бы один модификатор — Ctrl, Alt, Shift или Win. Если комбинация занята другой программой, поле подсветится.")
        self.hotkey_hint_label.setObjectName("Subtitle")
        self.hotkey_hint_label.setWordWrap(True)
        form.addRow("", self.hotkey_hint_label)
        # US-026: режим горячей клавиши (Переключатель / Push-to-Talk).
        self.hotkey_mode_combo = NoScrollComboBox()
        self.hotkey_mode_combo.addItem("Переключатель (нажать / нажать ещё раз)", "toggle")
        self.hotkey_mode_combo.addItem("Зажать и держать (Push-to-Talk)", "ptt")
        form.addRow("Режим клавиши", self.hotkey_mode_combo)
        self.hotkey_mode_hint_label = QLabel("Переключатель: нажать — запись, нажать снова — расшифровка. Push-to-Talk: держать — запись, отпустить — расшифровка. Для Push-to-Talk выбирайте комбинацию с Ctrl или Alt (например Ctrl+Space): одиночную клавишу пришлось бы удерживать всю диктовку, и она печаталась бы в текущем поле ввода.")
        self.hotkey_mode_hint_label.setObjectName("Subtitle")
        self.hotkey_mode_hint_label.setWordWrap(True)
        form.addRow("", self.hotkey_mode_hint_label)
        form.addRow("Вставка", self.auto_paste_check)
        form.addRow("Безопасная вставка", self.detect_text_field_check)
        # US-067: выбор целевого окна автовставки.
        self.paste_target_combo = NoScrollComboBox()
        self.paste_target_combo.addItem("В окно, активное на момент завершения записи", "on_finish")
        self.paste_target_combo.addItem("В окно, активное на момент начала записи", "on_start")
        form.addRow("Куда вставлять", self.paste_target_combo)
        self.paste_target_hint_label = QLabel(
            "«На момент завершения» — можно во время диктовки перейти в нужное поле, текст попадёт туда. "
            "«На момент начала» — текст попадёт только в то окно, где вы начали диктовать; если фокус сменился, "
            "вставка не выполняется, а текст остаётся в буфере обмена и в плашке."
        )
        self.paste_target_hint_label.setObjectName("Subtitle")
        self.paste_target_hint_label.setWordWrap(True)
        form.addRow("", self.paste_target_hint_label)
        form.addRow("Созвоны", self.meeting_compat_check)
        form.addRow("Плавающая плашка", self.overlay_enabled_check)
        form.addRow("Автозагрузка", self.autostart_check)
        form.addRow("Hugging Face token", self.hf_token_edit)
        form.addRow("Обновления", self.updates_enabled_check)
        form.addRow("GitHub repo", self.update_repo_edit)
        form.addRow("", self.updates_policy_label)
        microphone_row = QWidget()
        microphone_layout = QHBoxLayout(microphone_row)
        microphone_layout.setContentsMargins(0, 0, 0, 0)
        microphone_layout.addWidget(self.microphone_combo, 1)
        microphone_layout.addWidget(self.refresh_microphones_btn)
        form.addRow("Микрофон", microphone_row)
        form.addRow("Язык Whisper", self.language_combo)
        form.addRow("Ускорение Whisper", self.device_combo)
        form.addRow("Compute Whisper", self.compute_combo)

        # US-037: облачные реквизиты теперь живут в «Подключениях» на вкладке
        # «Модели». Раздел «Облачные модели» убран из «Настроек». Легаси-виджеты
        # STT (ключи/URL/модели/проверка/безопасный эндпоинт) сохранены СКРЫТО
        # для совместимости с обработчиками (check_cloud_connection,
        # _apply_cloud_models_to_settings_combo, US-018, load/save, миграция).
        # Управление облаком — через блок «Подключения» («Модели»). Initial Prompt,
        # длина чанка и fallback перенесены на «Модели» → «Облачные модели».
        self.cloud_stt_legacy_group = QWidget()
        self.cloud_stt_legacy_group.setVisible(False)
        _clg = QFormLayout(self.cloud_stt_legacy_group)
        _clg.setContentsMargins(0, 0, 0, 0)
        self.openai_stt_key_edit = QLineEdit()
        self.openai_stt_key_edit.setEchoMode(QLineEdit.Password)
        self.openai_stt_base_url_edit = QLineEdit()
        self.openai_safe_endpoint_check = QCheckBox("Внутренний безопасный эндпоинт Cloud.ru")
        self.openai_safe_endpoint_check.toggled.connect(self._on_openai_safe_endpoint_toggled)
        self.openai_stt_base_url_edit.textChanged.connect(self._update_safe_endpoint_checkbox_enabled)
        self.openai_stt_model_combo = EditableClickToOpenComboBox()
        self.openai_check_btn = QPushButton("Проверить соединение")
        self.openai_check_btn.clicked.connect(lambda: self.check_cloud_connection("openai"))
        self.elevenlabs_stt_key_edit = QLineEdit()
        self.elevenlabs_stt_key_edit.setEchoMode(QLineEdit.Password)
        self.elevenlabs_stt_model_combo = EditableClickToOpenComboBox()
        self.elevenlabs_check_btn = QPushButton("Проверить соединение")
        self.elevenlabs_check_btn.clicked.connect(lambda: self.check_cloud_connection("elevenlabs"))
        for _w in (
            self.openai_stt_key_edit, self.openai_stt_base_url_edit, self.openai_safe_endpoint_check,
            self.openai_stt_model_combo, self.openai_check_btn,
            self.elevenlabs_stt_key_edit, self.elevenlabs_stt_model_combo, self.elevenlabs_check_btn,
        ):
            _clg.addRow("", _w)
        form.addRow(self.cloud_stt_legacy_group)

        # US-020/US-037: блок «Улучшение расшифровки» перенесён на вкладку «Модели» → «Облачные модели».

        summary_separator = QLabel("Суммаризация")
        summary_separator.setObjectName("Title")
        summary_separator.setStyleSheet("font-size: 16px; margin-top: 12px;")
        form.addRow(summary_separator)
        # US-037: промпт суммаризации и Reasoning перенесены на «Модели» → «Облачные модели». В «Настройках» остаётся только выбор способа.

        # US-036: способ суммаризации — локально или облачная LLM.
        self.summary_mode_combo = NoScrollComboBox()
        self.summary_mode_combo.addItem("Локально (на этом компьютере)", "local")
        self.summary_mode_combo.addItem("Облако (OpenAI-совместимый API)", "cloud")
        self.summary_mode_combo.setToolTip(
            "Локально — через скачанную GGUF-модель. Облако — через OpenAI-совместимый "
            "API (быстрее, не нагружает компьютер, но передаёт текст на внешние серверы)."
        )
        self.summary_mode_combo.currentIndexChanged.connect(self._on_summary_mode_changed)
        form.addRow("Способ суммаризации", self.summary_mode_combo)

        # US-037: выбор подключения и модели суммаризации перенесён на «Модели» → «Облачные модели».
        # US-029: настройки REST API-сервера вынесены в отдельную вкладку «API» (см. _api_tab).
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
        self._refresh_updates_policy_state()
        buttons.addStretch(1)
        layout.addLayout(buttons)
        layout.addStretch(1)
        scroll.setWidget(tab)
        return scroll

    # ── Вкладка «API» (US-029, US-030) ─────────────────────────────────
    def _api_tab(self) -> QWidget:
        """US-029: отдельная вкладка с настройками REST API-сервера.
        US-030: поля предзаполнены значениями по умолчанию (хост 127.0.0.1,
        порт 8672, URL Swagger). Тёмный фон — через глобальное правило
        QWidget { background: #101114 } в APP_STYLE (см. _settings_tab)."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)
        form = QFormLayout()

        title = QLabel("REST API-сервер")
        title.setObjectName("Title")
        title.setStyleSheet("font-size: 16px;")
        form.addRow(title)

        self.api_enabled_check = QCheckBox("Включить REST API (требуется перезапуск)")
        self.api_enabled_check.setToolTip(
            "Запускает HTTP-сервер для приёма запросов на расшифровку от внешних приложений."
        )
        form.addRow("API", self.api_enabled_check)

        self.api_host_edit = QLineEdit()
        self.api_host_edit.setPlaceholderText("127.0.0.1")
        self.api_host_edit.setToolTip(
            "Хост (адрес) API-сервера. По умолчанию 127.0.0.1 (только локально). "
            "Укажите 0.0.0.0, чтобы принимать запросы из локальной сети."
        )
        form.addRow("Хост", self.api_host_edit)

        self.api_port_edit = QLineEdit()
        self.api_port_edit.setPlaceholderText("8672")
        self.api_port_edit.setToolTip("Порт для API-сервера. По умолчанию 8672.")
        form.addRow("Порт", self.api_port_edit)

        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        self.api_key_edit.setPlaceholderText("Оставьте пустым для открытого доступа")
        self.api_key_edit.setToolTip(
            "Bearer-токен для авторизации. Если пусто, API доступен без авторизации."
        )
        form.addRow("API-ключ", self.api_key_edit)

        self.api_swagger_url_label = QLabel()
        self.api_swagger_url_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.api_swagger_url_label.setToolTip("Адрес интерактивной документации API (Swagger UI).")
        form.addRow("Swagger UI", self.api_swagger_url_label)

        layout.addLayout(form)

        btns = QHBoxLayout()
        self.open_swagger_btn = QPushButton("Открыть Swagger")
        self.open_swagger_btn.setFocusPolicy(Qt.NoFocus)
        self.open_swagger_btn.setAutoDefault(False)
        self.open_swagger_btn.setToolTip("Открыть Swagger UI в браузере по текущему адресу сервера.")
        self.open_swagger_btn.clicked.connect(self.open_swagger)
        btns.addWidget(self.open_swagger_btn)
        btns.addStretch(1)
        layout.addLayout(btns)

        hint = QLabel(
            "Настройки сохраняются автоматически. Изменения хоста/порта применяются "
            "после перезапуска приложения. Если сервер запущен на 0.0.0.0, доступ к "
            "нему открыт для устройств в сети — используйте API-ключ."
        )
        hint.setWordWrap(True)
        hint.setObjectName("Subtitle")
        layout.addWidget(hint)
        layout.addStretch(1)

        # US-030: пересборка адреса Swagger при изменении хоста/порта.
        self.api_host_edit.textChanged.connect(self._update_swagger_url_label)
        self.api_port_edit.textChanged.connect(self._update_swagger_url_label)
        self._update_swagger_url_label()

        scroll.setWidget(tab)
        return scroll

    def _current_api_host(self) -> str:
        """US-030: текущий хост из поля или дефолт 127.0.0.1."""
        host = ""
        if hasattr(self, "api_host_edit"):
            host = self.api_host_edit.text().strip()
        if not host:
            host = (getattr(self.cfg, "api_host", "") or "").strip() or "127.0.0.1"
        # 0.0.0.0 слушает все интерфейсы, но открывать в браузере нужно localhost.
        return "127.0.0.1" if host == "0.0.0.0" else host

    def _current_api_port(self) -> int:
        """US-030: текущий порт из поля или дефолт 8672."""
        if hasattr(self, "api_port_edit"):
            raw = self.api_port_edit.text().strip()
            if raw.isdigit():
                return int(raw)
        return getattr(self.cfg, "api_port", 0) or 8672

    def _swagger_url(self) -> str:
        return f"http://{self._current_api_host()}:{self._current_api_port()}/docs"

    def _update_swagger_url_label(self) -> None:
        if hasattr(self, "api_swagger_url_label"):
            self.api_swagger_url_label.setText(self._swagger_url())

    def open_swagger(self) -> None:
        """US-029/US-030: открыть Swagger UI в браузере по актуальному адресу."""
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        url = self._swagger_url()
        if not self.cfg.api_enabled:
            self.status_label.setText(
                "API-сервер выключен — Swagger будет недоступен, пока вы не включите REST API "
                "и не перезапустите приложение."
            )
        QDesktopServices.openUrl(QUrl(url))

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
        # US-024: кнопка «Скачать» с выпадающим меню форматов экспорта
        from PySide6.QtWidgets import QMenu

        self.export_history_btn = QPushButton("Скачать ▾")
        self.export_history_btn.setToolTip("Сохранить выбранную расшифровку в файл TXT, Markdown или PDF.")
        export_menu = QMenu(self.export_history_btn)
        export_menu.addAction("Скачать как TXT", lambda: self.export_history_item("txt"))
        export_menu.addAction("Скачать как Markdown (.md)", lambda: self.export_history_item("md"))
        export_menu.addAction("Скачать как PDF", lambda: self.export_history_item("pdf"))
        self.export_history_btn.setMenu(export_menu)
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
        right.addWidget(self.export_history_btn)
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
        # US-054: надёжно разворачиваем окно из трея/свёрнутого состояния и
        # выводим на передний план — в т.ч. когда запрос пришёл от повторного
        # запуска ярлыка при свёрнутой в трей программе.
        self.showNormal()
        try:
            self.setWindowState((self.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
        except Exception:  # noqa: BLE001
            pass
        self.raise_()
        self.activateWindow()
        if sys.platform == "win32":
            # Windows не даёт фоновому процессу поднять окно вперёд; работает в
            # паре с AllowSetForegroundWindow(ASFW_ANY) во втором процессе.
            try:
                import ctypes
                ctypes.windll.user32.SetForegroundWindow(int(self.winId()))
            except Exception:  # noqa: BLE001
                pass

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
        _hm = getattr(self.cfg, "hotkey_mode", "toggle")
        _hm_idx = self.hotkey_mode_combo.findData(_hm)
        self.hotkey_mode_combo.setCurrentIndex(_hm_idx if _hm_idx >= 0 else 0)
        self.auto_paste_check.setChecked(self.cfg.auto_paste)
        self.detect_text_field_check.setChecked(self.cfg.paste_only_when_text_field_detected)
        _pt = getattr(self.cfg, "paste_target_window", "on_finish")
        _pt_idx = self.paste_target_combo.findData(_pt)
        self.paste_target_combo.setCurrentIndex(_pt_idx if _pt_idx >= 0 else 0)
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
            _s_prompt = getattr(self.cfg, "summary_system_prompt", "") or ""
            self.summary_prompt_edit.setPlainText(_s_prompt if _s_prompt.strip() else DEFAULT_SUMMARY_SYSTEM_PROMPT)
        if hasattr(self, "summary_mode_combo"):
            _smode = getattr(self.cfg, "summary_mode", "local") or "local"
            self.summary_mode_combo.blockSignals(True)
            _sidx = self.summary_mode_combo.findData(_smode)
            self.summary_mode_combo.setCurrentIndex(_sidx if _sidx >= 0 else 0)
            self.summary_mode_combo.blockSignals(False)
            self.summary_base_url_edit.setText(getattr(self.cfg, "summary_base_url", "") or "https://api.openai.com/v1")
            self.summary_key_edit.setText(getattr(self.cfg, "summary_api_key", "") or "")
            _smodel = getattr(self.cfg, "summary_model_id", "") or ""
            self.summary_model_combo.blockSignals(True)
            self.summary_model_combo.clear()
            if _smodel:
                self.summary_model_combo.addItem(_smodel, _smodel)
                self.summary_model_combo.setCurrentIndex(0)
            self.summary_model_combo.setEditText(_smodel)
            self.summary_model_combo.blockSignals(False)
            self.summary_reasoning_check.setChecked(bool(getattr(self.cfg, "summary_reasoning", False)))
            _s_eff = getattr(self.cfg, "summary_reasoning_effort", "low") or "low"
            _s_eff_idx = self.summary_reasoning_effort_combo.findData(_s_eff)
            self.summary_reasoning_effort_combo.setCurrentIndex(_s_eff_idx if _s_eff_idx >= 0 else 0)
            self._on_summary_reasoning_toggled(self.summary_reasoning_check.isChecked())
            self._update_summary_cloud_group_visibility()
        if hasattr(self, "api_enabled_check"):
            self.api_enabled_check.setChecked(self.cfg.api_enabled)
        if hasattr(self, "api_host_edit"):
            # US-030: показываем сохранённое значение; для дефолта оставляем поле
            # пустым (плейсхолдер 127.0.0.1), чтобы не путать с пользовательским вводом.
            _host = (getattr(self.cfg, "api_host", "") or "").strip()
            self.api_host_edit.setText("" if _host in ("", "127.0.0.1") else _host)
        if hasattr(self, "api_port_edit"):
            self.api_port_edit.setText(str(self.cfg.api_port) if self.cfg.api_port != 8672 else "")
        if hasattr(self, "api_key_edit"):
            self.api_key_edit.setText(self.cfg.api_key)
        if hasattr(self, "api_swagger_url_label"):
            self._update_swagger_url_label()
        # Cloud STT (US-015, US-016, US-032)
        if hasattr(self, "openai_stt_key_edit"):
            self.openai_stt_key_edit.setText(self.cfg.openai_stt_api_key)
            self.openai_stt_base_url_edit.setText(self.cfg.openai_stt_base_url or "https://api.openai.com/v1")
            # US-018: восстановить состояние чекбокса «безопасный эндпоинт Cloud.ru».
            if hasattr(self, "openai_safe_endpoint_check"):
                _ep_norm = normalize_endpoint(self.cfg.openai_stt_base_url or "")
                _safe_set = {normalize_endpoint(e) for e in getattr(self.cfg, "cloud_internal_safe_endpoints", [])}
                self.openai_safe_endpoint_check.setChecked(bool(_ep_norm) and _ep_norm in _safe_set)
                self._update_safe_endpoint_checkbox_enabled()
            self._fill_cloud_model_combo(self.openai_stt_model_combo, "openai", self.cfg.openai_stt_model_id)
            # US-035: загрузка Initial Prompt
            if hasattr(self, "openai_initial_prompt_edit"):
                self.openai_initial_prompt_edit.setPlainText(
                    self.cfg.openai_stt_initial_prompt if self.cfg.openai_stt_initial_prompt is not None else DEFAULT_OPENAI_INITIAL_PROMPT
                )
            self.elevenlabs_stt_key_edit.setText(self.cfg.elevenlabs_stt_api_key)
            self._fill_cloud_model_combo(self.elevenlabs_stt_model_combo, "elevenlabs", self.cfg.elevenlabs_stt_model_id)
            self.cloud_max_chunk_spin.setValue(max(30, min(300, int(self.cfg.cloud_max_chunk_seconds or 60))))
            # EPIC-10/US-039: вырезание тишины перед облаком
            if hasattr(self, "cloud_trim_silence_check"):
                self.cloud_trim_silence_check.setChecked(bool(getattr(self.cfg, "cloud_trim_silence_enabled", True)))
            if hasattr(self, "cloud_trim_aggr_slider"):
                try:
                    _agg_val = int(getattr(self.cfg, "cloud_trim_aggressiveness", 50))
                except (TypeError, ValueError):
                    _agg_val = 50
                _agg_val = max(0, min(100, _agg_val))
                self.cloud_trim_aggr_slider.setValue(_agg_val)
                self.cloud_trim_aggr_value.setText(str(_agg_val))
            self._fill_cloud_fallback_combo()
        # US-034: постобработка
        if hasattr(self, "postprocess_enabled_check"):
            self.postprocess_enabled_check.setChecked(bool(getattr(self.cfg, "postprocess_enabled", False)))
            self.postprocess_base_url_edit.setText(getattr(self.cfg, "postprocess_base_url", "") or "https://api.openai.com/v1")
            self.postprocess_key_edit.setText(getattr(self.cfg, "postprocess_api_key", "") or "")
            _pp_model = getattr(self.cfg, "postprocess_model_id", "") or ""
            self.postprocess_model_combo.blockSignals(True)
            self.postprocess_model_combo.clear()
            if _pp_model:
                self.postprocess_model_combo.addItem(_pp_model, _pp_model)
                self.postprocess_model_combo.setCurrentIndex(0)
            self.postprocess_model_combo.setEditText(_pp_model)
            self.postprocess_model_combo.blockSignals(False)
            _pp_prompt = getattr(self.cfg, "postprocess_system_prompt", None)
            self.postprocess_prompt_edit.setPlainText(
                _pp_prompt if _pp_prompt is not None else DEFAULT_POSTPROCESS_SYSTEM_PROMPT
            )
            self.postprocess_reasoning_check.setChecked(bool(getattr(self.cfg, "postprocess_reasoning", False)))
            _eff = getattr(self.cfg, "postprocess_reasoning_effort", "low") or "low"
            _eff_idx = self.postprocess_reasoning_effort_combo.findData(_eff)
            self.postprocess_reasoning_effort_combo.setCurrentIndex(_eff_idx if _eff_idx >= 0 else 0)
            self._on_postprocess_enabled_toggled(self.postprocess_enabled_check.isChecked())
            self._on_postprocess_reasoning_toggled(self.postprocess_reasoning_check.isChecked())

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
        for combo in [self.microphone_combo, self.language_combo, self.device_combo, self.compute_combo, self.hotkey_mode_combo, self.paste_target_combo]:
            combo.currentIndexChanged.connect(self.schedule_settings_autosave)
        self.file_stable_timestamps_check.stateChanged.connect(self.save_file_options)
        self.file_diarization_check.stateChanged.connect(self.save_file_options)
        self.file_speaker_count_combo.currentIndexChanged.connect(self.save_file_options)
        self.file_summary_check.stateChanged.connect(self.save_file_options)
        if hasattr(self, "summary_mode_combo"):
            self.summary_base_url_edit.editingFinished.connect(self.schedule_settings_autosave)
            self.summary_key_edit.editingFinished.connect(self.schedule_settings_autosave)
            self.summary_model_combo.editTextChanged.connect(self.schedule_settings_autosave)
            self.summary_model_combo.currentIndexChanged.connect(self.schedule_settings_autosave)
            self.summary_reasoning_check.stateChanged.connect(self.schedule_settings_autosave)
            self.summary_reasoning_effort_combo.currentIndexChanged.connect(self.schedule_settings_autosave)
        # TASK-061 (US-017): реактивность опций таймкодов/диаризации к смене модели файла.
        self.file_model_combo.currentIndexChanged.connect(self._update_file_options_for_model)
        self.api_enabled_check.stateChanged.connect(self.schedule_settings_autosave)
        self.api_host_edit.editingFinished.connect(self.schedule_settings_autosave)
        self.api_port_edit.editingFinished.connect(self.schedule_settings_autosave)
        self.api_key_edit.editingFinished.connect(self.schedule_settings_autosave)
        # Cloud STT (US-015, US-016, US-032). Изменение ключа/URL/модели
        # триггерит autosave + перестроение реестра cloud-моделей.
        if hasattr(self, "openai_stt_key_edit"):
            self.openai_stt_key_edit.editingFinished.connect(self.on_cloud_settings_changed)
            self.openai_stt_base_url_edit.editingFinished.connect(self.on_cloud_settings_changed)
            self.openai_stt_model_combo.editTextChanged.connect(self.schedule_settings_autosave)
            self.openai_stt_model_combo.currentIndexChanged.connect(self.schedule_settings_autosave)
            # US-035: автосейв Initial Prompt при изменении
            if hasattr(self, "openai_initial_prompt_edit"):
                self.openai_initial_prompt_edit.textChanged.connect(self.schedule_settings_autosave)
            self.elevenlabs_stt_key_edit.editingFinished.connect(self.on_cloud_settings_changed)
            self.elevenlabs_stt_model_combo.editTextChanged.connect(self.schedule_settings_autosave)
            self.elevenlabs_stt_model_combo.currentIndexChanged.connect(self.schedule_settings_autosave)
            self.cloud_max_chunk_spin.valueChanged.connect(self.schedule_settings_autosave)
            self.cloud_fallback_combo.currentIndexChanged.connect(self.schedule_settings_autosave)
            # EPIC-10/US-039: автосейв вырезания тишины
            if hasattr(self, "cloud_trim_silence_check"):
                self.cloud_trim_silence_check.toggled.connect(self.schedule_settings_autosave)
            if hasattr(self, "cloud_trim_aggr_slider"):
                self.cloud_trim_aggr_slider.valueChanged.connect(self.schedule_settings_autosave)
        # US-034: автосейв постобработки
        if hasattr(self, "postprocess_enabled_check"):
            self.postprocess_enabled_check.stateChanged.connect(self.schedule_settings_autosave)
            self.postprocess_reasoning_check.stateChanged.connect(self.schedule_settings_autosave)
            self.postprocess_base_url_edit.editingFinished.connect(self.schedule_settings_autosave)
            self.postprocess_key_edit.editingFinished.connect(self.schedule_settings_autosave)
            self.postprocess_model_combo.editTextChanged.connect(self.schedule_settings_autosave)
            self.postprocess_model_combo.currentIndexChanged.connect(self.schedule_settings_autosave)
            self.postprocess_prompt_edit.textChanged.connect(self.schedule_settings_autosave)
            self.postprocess_reasoning_effort_combo.currentIndexChanged.connect(self.schedule_settings_autosave)

    def on_reset_openai_initial_prompt(self) -> None:
        """US-035: сбросить поле Initial Prompt к дефолтному значению."""
        if not hasattr(self, "openai_initial_prompt_edit"):
            return
        # US-035 diag: entry + before/after значения для каждого шага.
        before_widget = self.openai_initial_prompt_edit.toPlainText()
        before_cfg = getattr(self.cfg, "openai_stt_initial_prompt", "")
        log.info(
            "on_reset_openai_initial_prompt: ENTRY widget_chars=%d cfg_chars=%d default_chars=%d",
            len(before_widget), len(before_cfg), len(DEFAULT_OPENAI_INITIAL_PROMPT),
        )
        self.openai_initial_prompt_edit.setPlainText(DEFAULT_OPENAI_INITIAL_PROMPT)
        after_setplain_widget = self.openai_initial_prompt_edit.toPlainText()
        log.info(
            "on_reset_openai_initial_prompt: AFTER setPlainText widget_chars=%d preview=%r",
            len(after_setplain_widget), after_setplain_widget[:60].replace("\n", " "),
        )
        # Останавливаем pending debounce таймер от textChanged
        try:
            self._settings_save_timer.stop()
        except Exception:  # noqa: BLE001
            pass
        self.save_settings(auto=True)
        after_save_cfg = getattr(self.cfg, "openai_stt_initial_prompt", "")
        log.info(
            "on_reset_openai_initial_prompt: AFTER save_settings cfg_chars=%d preview=%r",
            len(after_save_cfg), after_save_cfg[:60].replace("\n", " "),
        )

    def save_file_options(self) -> None:
        if self._settings_loading:
            return
        self.cfg.file_stable_timestamps_enabled = self.file_stable_timestamps_check.isChecked()
        self.cfg.file_diarization_enabled = self.file_diarization_check.isChecked()
        self.cfg.file_speaker_count = str(self.file_speaker_count_combo.currentData() or "auto")
        if hasattr(self, "file_summary_check"):
            self.cfg.summary_enabled = self.file_summary_check.isChecked()
            # US-018 (guarded-хук): сейчас суммаризация локальная (llama-cpp, CPU),
            # поэтому is_cloud_model_key(...) == False и уведомление НЕ показывается.
            # Когда/если появится облачная суммаризация (cloud:* модель суммаризации) —
            # при её включении сработает то же предупреждение о передаче данных.
            if self.cfg.summary_enabled and is_cloud_model_key(
                getattr(self.cfg, "selected_summary_model", "") or ""
            ):
                if not self._confirm_cloud_switch("summary"):
                    self.cfg.summary_enabled = False
                    self.file_summary_check.blockSignals(True)
                    self.file_summary_check.setChecked(False)
                    self.file_summary_check.blockSignals(False)
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
                self.hotkey_hint_label.setText("Кликните поле и нажмите новую комбинацию. Нужен хотя бы один модификатор — Ctrl, Alt, Shift или Win. Если комбинация занята другой программой, поле подсветится.")
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
            self.hotkey.start(self.cfg.hotkey, mode=getattr(self.cfg, "hotkey_mode", "toggle"))
            self.status_label.setText(f"Готово. Горячая клавиша: {self.cfg.hotkey}")
            self.overlay.set_hotkey(self.cfg.hotkey)
            self._set_hotkey_attention(False)
            self._hotkey_error_message = ""
            return True
        except Exception as exc:  # noqa: BLE001
            log.exception("Hotkey registration failed")
            message = str(exc)
            self._hotkey_error_message = message
            self.status_label.setText(message)
            self._set_hotkey_attention(True, "Комбинация не зарегистрировалась. Нажмите поле и выберите другую, например Ctrl+Alt+Space.")
            if show_errors:
                QMessageBox.warning(self, "Горячая клавиша", message)
            return False

    def _notify_hotkey_registration_failed(self) -> None:
        """TASK-334 (US-064): заметное уведомление о нерабочей комбинации.

        Сохранённую комбинацию могла занять другая программа, или она пришла из
        старых настроек в неподдерживаемом виде. Раньше об этом сообщала только
        строка статуса — её не замечали, и выглядело это как «диктовка сломалась».
        Теперь: уведомление в трее + модальное окно с переходом на вкладку
        настроек к подсвеченному полю.
        """
        message = getattr(self, "_hotkey_error_message", "") or "Комбинацию не удалось зарегистрировать."
        try:
            self.tray.showMessage(
                "Voice Input Local",
                f"Горячая клавиша {self.cfg.hotkey} не работает. Задайте другую комбинацию в настройках.",
                QSystemTrayIcon.Warning,
                10000,
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            self.tabs.setCurrentIndex(3)  # 0 Диктовка,1 Файлы,2 Модели,3 Настройки
            self.hotkey_edit.setFocus()
        except Exception:  # noqa: BLE001
            pass
        QMessageBox.warning(
            self,
            "Горячая клавиша не работает",
            f"Комбинация {self.cfg.hotkey} не зарегистрирована — диктовка по горячей клавише сейчас недоступна.\n\n"
            f"{message}\n\n"
            "Нажмите подсвеченное поле «Горячая клавиша» на вкладке «Настройки» и задайте другую комбинацию, "
            "например Ctrl+Alt+Space.",
        )

    def start_escape_watch(self) -> None:
        """US-065: включить отмену по Escape на время операции.

        Наблюдение точечное: опрашивается ровно один виртуальный код и только
        пока идёт запись или расшифровка. Клавиша не поглощается — то же
        нажатие как обычно доходит до активной программы.
        """
        if self.escape_watcher.is_active:
            return
        try:
            self.escape_watcher.start([VK_ESCAPE], on_press=lambda _vk: self.cancel_signal.triggered.emit())
        except Exception:  # noqa: BLE001
            log.exception("Escape watch start failed")

    def stop_escape_watch(self) -> None:
        """US-065: снять наблюдение за Escape. Зовётся во всех точках выхода."""
        try:
            self.escape_watcher.stop()
        except Exception:  # noqa: BLE001
            pass

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

    # --- US-018: уведомления безопасности при переключении на облачные модели ---
    def _cloud_endpoint_for_provider(self, provider: str) -> str:
        """base_url провайдера для проверки cloud.ru и показа в диалоге."""
        if provider == "openai":
            return getattr(self.cfg, "openai_stt_base_url", "") or ""
        if provider == "elevenlabs":
            return "https://api.elevenlabs.io"
        if provider == "postprocess":
            conn = self.cfg.connection_by_id(getattr(self.cfg, "postprocess_connection_id", "") or "")
            return (conn.base_url if conn else getattr(self.cfg, "postprocess_base_url", "")) or ""
        if provider == "summary":
            conn = self.cfg.connection_by_id(getattr(self.cfg, "summary_connection_id", "") or "")
            return (conn.base_url if conn else getattr(self.cfg, "summary_base_url", "")) or ""
        return ""

    def _cloud_provider_label(self, provider: str) -> str:
        return {
            "openai": "OpenAI-совместимый STT",
            "elevenlabs": "ElevenLabs STT",
            "postprocess": "Постобработка (облачная LLM)",
            "summary": "Суммаризация (облачная LLM)",
        }.get(provider, provider)

    def _endpoint_marked_safe(self, endpoint: str) -> bool:
        """True, если пользователь пометил этот эндпоинт безопасным (и это cloud.ru)."""
        norm = normalize_endpoint(endpoint)
        if not norm or not host_is_cloudru(endpoint):
            return False
        saved = {normalize_endpoint(e) for e in getattr(self.cfg, "cloud_internal_safe_endpoints", [])}
        return norm in saved

    def _confirm_cloud_endpoint(self, label: str, endpoint: str, sess_prefix: str) -> bool:
        """US-018: показать нужное уведомление для конкретного облачного ЭНДПОИНТА.

        True — продолжать; False — пользователь отменил (только для внешней
        модели). Безопасные внутренние эндпоинты Cloud.ru → крупное зелёное
        подтверждение однократно за сессию. Внешние → янтарное предупреждение,
        подавляется в рамках сессии. Безопасность привязана к ЭНДПОИНТУ
        (base_url подключения), а не к провайдеру — работает per-connection.
        """
        sess_key = f"{sess_prefix}|{normalize_endpoint(endpoint)}"
        if self._endpoint_marked_safe(endpoint):
            if sess_key not in self._cloud_safe_confirmed_session:
                confirm_safe_switch(self, provider_label=label, endpoint=endpoint)
                self._cloud_safe_confirmed_session.add(sess_key)
            return True
        if sess_key in self._cloud_warned_session:
            return True
        can_mark_safe = host_is_cloudru(endpoint)
        accepted, mark_safe = confirm_external_switch(
            self, provider_label=label, endpoint=endpoint, can_mark_safe=can_mark_safe
        )
        if not accepted:
            return False
        if mark_safe:
            lst = list(getattr(self.cfg, "cloud_internal_safe_endpoints", []))
            norm = normalize_endpoint(endpoint)
            if norm and norm not in {normalize_endpoint(x) for x in lst}:
                lst.append(endpoint.strip())
                self.cfg.cloud_internal_safe_endpoints = lst
                self.cfg.save()
            self._cloud_safe_confirmed_session.add(sess_key)
        else:
            self._cloud_warned_session.add(sess_key)
        return True

    def _confirm_cloud_switch(self, provider: str) -> bool:
        """US-018: уведомление при переходе на облачную функцию (postprocess/summary/STT).
        Эндпоинт берётся из подключения функции (или legacy base_url)."""
        endpoint = self._cloud_endpoint_for_provider(provider)
        label = self._cloud_provider_label(provider)
        return self._confirm_cloud_endpoint(label, endpoint, provider)

    def _confirm_cloud_model_switch(self, key: str) -> bool:
        """US-018: смена МОДЕЛИ диктовки. Эндпоинт берётся из подключения,
        на которое ссылается cloud-ключ (cloud:<connection_id>:<model_id>)."""
        if not is_cloud_model_key(key):
            return True
        from .models import resolve_cloud_connection
        conn = resolve_cloud_connection(self.cfg, key)
        if conn is None:
            return True
        endpoint = conn.base_url if conn.type == "openai" else "https://api.elevenlabs.io"
        label = conn.name or ("ElevenLabs STT" if conn.type == "elevenlabs" else "Облачная STT")
        return self._confirm_cloud_endpoint(label, endpoint, "dictation")

    def _update_safe_endpoint_checkbox_enabled(self) -> None:
        """US-018: чекбокс «безопасный эндпоинт» активен только для домена cloud.ru."""
        if not hasattr(self, "openai_safe_endpoint_check"):
            return
        url = self.openai_stt_base_url_edit.text().strip()
        is_cloudru = host_is_cloudru(url)
        self.openai_safe_endpoint_check.setEnabled(is_cloudru)
        if not is_cloudru and self.openai_safe_endpoint_check.isChecked():
            self.openai_safe_endpoint_check.blockSignals(True)
            self.openai_safe_endpoint_check.setChecked(False)
            self.openai_safe_endpoint_check.blockSignals(False)

    def _on_openai_safe_endpoint_toggled(self, checked: bool) -> None:
        """US-018: пользователь вручную помечает/снимает безопасный эндпоинт Cloud.ru."""
        if getattr(self, "_settings_loading", False):
            return
        url = self.openai_stt_base_url_edit.text().strip()
        norm = normalize_endpoint(url)
        lst = list(getattr(self.cfg, "cloud_internal_safe_endpoints", []))
        if checked:
            if not host_is_cloudru(url):
                self.openai_safe_endpoint_check.blockSignals(True)
                self.openai_safe_endpoint_check.setChecked(False)
                self.openai_safe_endpoint_check.blockSignals(False)
                return
            if norm and norm not in {normalize_endpoint(x) for x in lst}:
                lst.append(url)
        else:
            lst = [x for x in lst if normalize_endpoint(x) != norm]
        self.cfg.cloud_internal_safe_endpoints = lst
        # Статус безопасности openai изменился — сбросить его сессионные подтверждения.
        self._cloud_warned_session = {k for k in self._cloud_warned_session if not k.startswith("openai|")}
        self._cloud_safe_confirmed_session = {k for k in self._cloud_safe_confirmed_session if not k.startswith("openai|")}
        self.cfg.save()

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
        if is_cloud_model_key(key) and not self._confirm_cloud_model_switch(key):
            # US-018: пользователь отменил переход на облачную модель —
            # вернуть combo к текущей модели (guard выше предотвращает рекурсию).
            self._set_combo_value(self.model_combo, self.cfg.selected_model)
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
        if is_cloud_model_key(key) and not self._confirm_cloud_model_switch(key):
            self.refresh_available_models_combo()  # US-018: отмена — вернуть прежнюю модель в combo
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
        if is_cloud_model_key(key) and not self._confirm_cloud_model_switch(key):
            return  # US-018: пользователь отменил переход на облачную модель
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

    def _app_is_busy(self) -> bool:
        # US-048: единый признак «идёт активная работа, которую нельзя прерывать»
        # централизованным обновлением: запись/диктовка, расшифровка файла,
        # суммаризация.
        if self.is_dictation_busy() or self.is_file_busy():
            return True
        worker = getattr(self, "summarize_worker", None)
        if worker is not None:
            try:
                if worker.isRunning():
                    return True
            except RuntimeError:
                pass
        return False

    def _update_busy_marker(self) -> None:
        # US-048: тик таймера — держим/снимаем маркер занятости по факту работы.
        try:
            if self._app_is_busy():
                busy_marker.write_heartbeat()
            else:
                busy_marker.clear()
        except Exception:  # noqa: BLE001
            pass
        # US-057: в том же тике проверяем, не ждёт ли централизованное обновление.
        self._check_pending_update()
        # US-058: и не просит ли установщик закрыться для тихого обновления (простой).
        self._check_idle_update_close()

    def _check_pending_update(self) -> None:
        # US-057: установщик под SYSTEM не может показать окно — это делает
        # приложение. Если пришло централизованное обновление (маркер
        # update-pending) И идёт активная работа — спрашиваем пользователя.
        # При простое окно не нужно: установщик поставит обновление тихо.
        if self._update_decision_open:
            return
        try:
            if not update_signal.is_update_pending():
                return
            if not self._app_is_busy():
                return
        except Exception:  # noqa: BLE001
            return
        self._update_decision_open = True
        try:
            box = QMessageBox(self)
            box.setWindowTitle("Централизованное обновление")
            box.setIcon(QMessageBox.Question)
            box.setText(
                "Готово обновление Voice Input Local, но сейчас идёт активная работа.\n\n"
                "• «Закрыть и обновить» — приложение закроется, и обновление установится "
                "автоматически в течение нескольких минут (текущая работа будет прервана).\n"
                "• «Отклонить» — продолжить работу; обновление придёт позже."
            )
            update_btn = box.addButton("Закрыть и обновить", QMessageBox.AcceptRole)
            box.addButton("Отклонить", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is update_btn:
                self._accept_centralized_update()
            else:
                self._decline_centralized_update()
        except Exception:  # noqa: BLE001
            log.exception("Ошибка диалога централизованного обновления")
        finally:
            self._update_decision_open = False

    def _accept_centralized_update(self) -> None:
        # US-057/US-059: пользователь согласился прервать работу ради обновления.
        # Единый single-pass: снимаем маркеры, показываем сообщение и закрываемся.
        # Установщик (он ждёт в ЭТОМ ЖЕ прогоне, US-059) тут же ставит обновление.
        # US-061: приложение НЕ запускает никаких процессов (powershell-релончер
        # убран — антивирус расценивал его как троянское поведение). Возврат —
        # штатным автозапуском Windows при следующем входе, если он включён.
        try:
            update_signal.clear_update_pending()
            update_signal.clear_update_decline()
            busy_marker.clear()
        except Exception:  # noqa: BLE001
            pass
        log.info("Централизованное обновление принято — закрываю приложение (US-059).")
        try:
            self.status_label.setText("Устанавливается обновление, приложение закроется…")
        except Exception:  # noqa: BLE001
            pass
        try:
            self.tray.showMessage(
                "Voice Input Local",
                "Устанавливается обновление, приложение закроется…",
                QSystemTrayIcon.Information,
                4000,
            )
        except Exception:  # noqa: BLE001
            pass
        # Короткая пауза, чтобы пользователь увидел сообщение, затем полный выход.
        QTimer.singleShot(1500, self.really_quit)

    def _decline_centralized_update(self) -> None:
        # US-059: пользователь отложил. Пишем update-decline — установщик в ЭТОМ ЖЕ
        # прогоне прекращает ждать и выходит кодом 101 (KSC повторит). Снимаем pending,
        # чтобы окно не всплыло сразу снова. Отдельного кода «отклонено» для KSC нет.
        try:
            update_signal.set_update_decline()
            update_signal.clear_update_pending()
        except Exception:  # noqa: BLE001
            pass
        self.status_label.setText("Обновление отложено — продолжайте работу.")

    def _check_idle_update_close(self) -> None:
        # US-058: при ПРОСТОЕ установщик (под SYSTEM, сессия 0) не может закрыть
        # трей-приложение пользовательской сессии кросс-сессионно, поэтому сигналит
        # маркером update-close, а приложение закрывается САМО. US-061: возврат —
        # штатным автозапуском Windows при следующем входе (powershell-релончер
        # убран, т.к. антивирус расценивал его как троянское поведение). Только
        # frozen-сборка и только при простое.
        if getattr(self, "_idle_update_closing", False) or getattr(self, "_quitting", False):
            return
        if getattr(self, "_update_decision_open", False):
            return
        if not getattr(sys, "frozen", False):
            return
        try:
            if not update_signal.is_update_close():
                return
            if self._app_is_busy():
                # Стал занят после сигнала — не закрываемся; установщик отложит
                # (таймаут -> код 101), KSC повторит позже.
                return
        except Exception:  # noqa: BLE001
            return
        self._idle_update_closing = True
        log.info("US-058: сигнал update-close при простое — закрываюсь для обновления.")
        try:
            self.status_label.setText("Устанавливается обновление, приложение закроется…")
        except Exception:  # noqa: BLE001
            pass
        try:
            self.tray.showMessage(
                "Voice Input Local",
                "Устанавливается обновление, приложение закроется…",
                QSystemTrayIcon.Information,
                4000,
            )
        except Exception:  # noqa: BLE001
            pass
        # Короткая пауза, чтобы пользователь увидел сообщение, затем полный выход.
        QTimer.singleShot(1500, self.really_quit)

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

    def _update_file_options_for_model(self) -> None:
        """TASK-061 (US-017): реактивность чекбоксов «Точные таймкоды» и
        «Диаризация» при смене модели на вкладке «Файлы».

        Cloud-модели отдают таймкоды нативно (verbose_json у OpenAI, words у
        ElevenLabs) — дополнительная локальная VAD-модель им не нужна. Диаризация
        для OpenAI выполняется локальным пост-процессом на CPU, для ElevenLabs —
        нативно (diarize=true). Для локальных моделей — прежнее поведение.
        """
        if not hasattr(self, "file_diarization_hint"):
            return
        key = self._file_model_key()
        is_cloud = is_cloud_model_key(key)
        provider = cloud_provider_of(key) if is_cloud else ""
        # Таймкоды: cloud не требует доп. VAD-модель.
        if is_cloud:
            self.file_stable_timestamps_check.setToolTip(
                "Cloud-модель отдаёт таймкоды нативно — дополнительная VAD-модель не нужна."
            )
        else:
            self.file_stable_timestamps_check.setToolTip(
                "Выключено по умолчанию. Улучшает разбивку фраз и таймкоды при обработке файлов."
            )
        # Подсказка по способу диаризации.
        hint = ""
        if is_cloud and provider == "openai":
            hint = "Диаризация выполнится локально на CPU (+5–30 сек)."
        elif is_cloud and provider == "elevenlabs":
            hint = "Диаризация выполнится нативно на стороне ElevenLabs."
        self.file_diarization_hint.setText(hint)
        self.file_diarization_hint.setVisible(bool(hint))

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
            # US-018: предупреждение/подтверждение о передаче файла в облако.
            if not self._confirm_cloud_model_switch(key):
                return
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
        # TASK-061 (US-017): доп. локальные модели (VAD, Sortformer) нужны ТОЛЬКО
        # для локальной расшифровки. Cloud-модели отдают таймкоды нативно, а
        # диаризация для cloud выполняется иначе (OpenAI — локальный пост-процесс
        # через diarization.py без Sortformer; ElevenLabs — нативно diarize=true).
        if not is_cloud_model_key(key):
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
        self._last_file_overlay_text = f"Файл · {progress.percent}%"
        if self.cfg.overlay_enabled:
            # TASK-126 (US-019): overlay не перезаписывается прогрессом файла, если
            # сейчас идёт диктовка, показан результат ИЛИ открыт пикер выбора
            # облачной модели (иначе список исчезал бы через 1-2 сек, не дав выбрать).
            if self.is_dictation_busy() or getattr(self, "result_preview_active", False) or self.overlay.is_in_picker():
                # Overlay обслуживает диктовку / результат / пикер — НЕ трогаем его.
                # Прогресс файла виден на вкладке «Файлы» (file_progress + file_status_label).
                pass
            else:
                self.overlay.show_processing(self._last_file_overlay_text)

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

    def _format_file_transcript_for_history(self) -> str:
        """US-036: собрать форматированный текст расшифровки файла (тайм-коды +
        спикеры) из накопленных блоков — той же разметкой, что на вкладке «Файлы».
        Используется для сохранения в историю, чтобы разметка не терялась и была
        доступна суммаризации. Пустая строка — если блоков/разметки нет."""
        blocks = getattr(self, "_file_transcript_blocks", None) or []
        if not blocks:
            return ""
        show_ts = bool(getattr(self, "_file_show_timestamps_for_job", False))
        has_speakers = any((b.get("speaker") or "").strip() for b in blocks)
        if not show_ts and not has_speakers:
            return ""
        lines = []
        for b in blocks:
            prefix = ""
            if show_ts:
                prefix += f"[{format_duration(b.get('start', 0.0))}–{format_duration(b.get('end', 0.0))}] "
            sp = (b.get("speaker") or "").strip()
            if sp:
                prefix += f"{sp}: "
            txt = (b.get("text") or "").strip()
            if txt:
                lines.append(prefix + txt)
        return "\n".join(lines)

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
        # TASK-084 (US-019): defensive-перерегистрация hotkey при клике cancel.
        # Прежняя причина — клавиатурный хук терялся во время длительной
        # cloud-операции — с переходом на системную регистрацию (EPIC-13)
        # исчезла. Вызов сохранён и стал безопасным no-op: HotkeyService не
        # трогает уже зарегистрированную ту же комбинацию.
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
            # US-036: сохраняем в историю форматированный текст (тайм-коды/спикеры),
            # если они были включены — иначе суммаризация из истории теряет разметку.
            history_text = self._format_file_transcript_for_history() or text
            self.history.add(model_key, duration, False, history_text, source="file", file_name=path.name, file_path=str(path), segments_json=segments_json)
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
        # SUM-01: Auto-summarize if checkbox is checked.
        # US-037: суммаризируем тот же текст, что сохранён в историю (с разметкой
        # тайм-кодов/спикеров) — чтобы поведение совпадало с суммаризацией из «Истории».
        if text and self.file_summary_check.isChecked():
            self._start_file_summary(self._format_file_transcript_for_history() or text)

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
        # TASK-081 (US-019): defensive перерегистрация hotkey. С системной
        # регистрацией (EPIC-13) терять нечего — вызов остаётся безопасным
        # no-op и сохранён, чтобы не менять поведение соседних веток.
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
        old_hotkey_mode = getattr(self.cfg, "hotkey_mode", "toggle")
        requested_hotkey_mode = str(self.hotkey_mode_combo.currentData() or "toggle")
        self.cfg.auto_paste = self.auto_paste_check.isChecked()
        self.cfg.paste_only_when_text_field_detected = self.detect_text_field_check.isChecked()
        self.cfg.paste_target_window = str(self.paste_target_combo.currentData() or "on_finish")
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
        if hasattr(self, "summary_mode_combo"):
            old_sum_key = getattr(self.cfg, "summary_api_key", "")
            old_sum_url = getattr(self.cfg, "summary_base_url", "")
            self.cfg.summary_mode = str(self.summary_mode_combo.currentData() or "local")
            self.cfg.summary_api_key = self.summary_key_edit.text().strip()
            self.cfg.summary_base_url = (self.summary_base_url_edit.text().strip() or "https://api.openai.com/v1")
            self.cfg.summary_model_id = str(self.summary_model_combo.currentText() or "").strip()
            if hasattr(self, "summary_conn_combo"):
                self.cfg.summary_connection_id = str(self.summary_conn_combo.currentData() or "")
            self.cfg.summary_reasoning = self.summary_reasoning_check.isChecked()
            _sum_eff = str(self.summary_reasoning_effort_combo.currentData() or "low")
            self.cfg.summary_reasoning_effort = _sum_eff if _sum_eff in ("low", "medium", "high") else "low"
            if old_sum_key != self.cfg.summary_api_key or old_sum_url != self.cfg.summary_base_url:
                try:
                    from . import cloud_llm as _cl_sum
                    _cl_sum.invalidate_discover_cache()
                except Exception:  # noqa: BLE001
                    pass
                self._cloud_warned_session = {k for k in self._cloud_warned_session if not k.startswith("summary|")}
                self._cloud_safe_confirmed_session = {k for k in self._cloud_safe_confirmed_session if not k.startswith("summary|")}
        if hasattr(self, "api_enabled_check"):
            self.cfg.api_enabled = self.api_enabled_check.isChecked()
        if hasattr(self, "api_host_edit"):
            # US-030: пусто → дефолт 127.0.0.1.
            self.cfg.api_host = self.api_host_edit.text().strip() or "127.0.0.1"
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
            # US-035: сохранение Initial Prompt (пустая строка означает «не передавать prompt»)
            if hasattr(self, "openai_initial_prompt_edit"):
                _new_prompt = self.openai_initial_prompt_edit.toPlainText().strip()
                _old_prompt = self.cfg.openai_stt_initial_prompt
                self.cfg.openai_stt_initial_prompt = _new_prompt
                # US-035 diag: лог изменения prompt при save_settings
                if _new_prompt != _old_prompt:
                    log.info(
                        "save_settings: openai_stt_initial_prompt CHANGED old_chars=%d new_chars=%d new_preview=%r auto=%s",
                        len(_old_prompt or ""), len(_new_prompt), _new_prompt[:60].replace("\n", " "), auto,
                    )
                else:
                    log.info(
                        "save_settings: openai_stt_initial_prompt same chars=%d auto=%s",
                        len(_new_prompt), auto,
                    )
            self.cfg.elevenlabs_stt_api_key = self.elevenlabs_stt_key_edit.text().strip()
            self.cfg.elevenlabs_stt_model_id = str(self.elevenlabs_stt_model_combo.currentText() or "").strip()
            self.cfg.cloud_max_chunk_seconds = int(self.cloud_max_chunk_spin.value())
            # EPIC-10/US-039: вырезание тишины перед облаком
            if hasattr(self, "cloud_trim_silence_check"):
                self.cfg.cloud_trim_silence_enabled = bool(self.cloud_trim_silence_check.isChecked())
            if hasattr(self, "cloud_trim_aggr_slider"):
                self.cfg.cloud_trim_aggressiveness = int(self.cloud_trim_aggr_slider.value())
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
                # US-018: сменились ключ/URL STT-провайдера → сбросить сессионное
                # подавление уведомлений (смена провайдера должна показать снова).
                self._cloud_warned_session.clear()
                self._cloud_safe_confirmed_session.clear()
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
        # US-034: постобработка облачной LLM
        if hasattr(self, "postprocess_enabled_check"):
            old_pp_key = getattr(self.cfg, "postprocess_api_key", "")
            old_pp_url = getattr(self.cfg, "postprocess_base_url", "")
            self.cfg.postprocess_enabled = self.postprocess_enabled_check.isChecked()
            self.cfg.postprocess_api_key = self.postprocess_key_edit.text().strip()
            self.cfg.postprocess_base_url = (self.postprocess_base_url_edit.text().strip() or "https://api.openai.com/v1")
            if hasattr(self, "postprocess_conn_combo"):
                self.cfg.postprocess_connection_id = str(self.postprocess_conn_combo.currentData() or "")
            self.cfg.postprocess_model_id = str(self.postprocess_model_combo.currentText() or "").strip()
            self.cfg.postprocess_system_prompt = self.postprocess_prompt_edit.toPlainText().strip()
            self.cfg.postprocess_reasoning = self.postprocess_reasoning_check.isChecked()
            _eff = str(self.postprocess_reasoning_effort_combo.currentData() or "low")
            self.cfg.postprocess_reasoning_effort = _eff if _eff in ("low", "medium", "high") else "low"
            if old_pp_key != self.cfg.postprocess_api_key or old_pp_url != self.cfg.postprocess_base_url:
                try:
                    from . import cloud_llm as _cl
                    _cl.invalidate_discover_cache()
                except Exception:  # noqa: BLE001
                    pass
                # US-018: сменились ключ/URL LLM постобработки → сбросить её
                # сессионные подтверждения, чтобы предупреждение показалось снова.
                self._cloud_warned_session = {k for k in self._cloud_warned_session if not k.startswith("postprocess|")}
                self._cloud_safe_confirmed_session = {k for k in self._cloud_safe_confirmed_session if not k.startswith("postprocess|")}
        # US-044: словарь терминов постобработки (данные из таблицы вкладки «Словарь»).
        if hasattr(self, "dictionary_table"):
            self.cfg.postprocess_glossary = self._collect_dictionary_from_table()
        # US-046: мастер-тумблер словаря.
        if hasattr(self, "postprocess_glossary_enabled_check"):
            self.cfg.postprocess_glossary_enabled = self.postprocess_glossary_enabled_check.isChecked()
        self.cfg.language = str(self.language_combo.currentData())
        self.cfg.device = str(self.device_combo.currentData())
        self.cfg.compute_type = str(self.compute_combo.currentData())
        hotkey_error = None
        # US-026: применяем смену комбинации И/ИЛИ режима без перезапуска.
        self.cfg.hotkey_mode = requested_hotkey_mode
        if requested_hotkey != old_hotkey or requested_hotkey_mode != old_hotkey_mode:
            # Do not persist a broken hotkey. The HotkeyService keeps the old
            # handle active if the new combo cannot be parsed/registered.
            previous = self.cfg.hotkey
            self.cfg.hotkey = requested_hotkey
            if not self.register_hotkey(show_errors=not auto):
                hotkey_error = requested_hotkey
                self.cfg.hotkey = previous
                self.cfg.hotkey_mode = old_hotkey_mode
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

    def _refresh_updates_policy_state(self) -> None:
        # US-047: машинная политика (HKLM\SOFTWARE\Policies\VoiceInputLocal)
        # централизованно отключает встроенный апдейтер с приоритетом над
        # пользовательской настройкой. Блокируем контролы и показываем пометку.
        disabled = updates_disabled_by_policy()
        for name in ("updates_enabled_check", "update_repo_edit", "check_updates_btn"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.setEnabled(not disabled)
        label = getattr(self, "updates_policy_label", None)
        if label is not None:
            label.setVisible(disabled)

    def check_for_updates(self, *, manual: bool = False) -> None:
        if updates_disabled_by_policy():
            # US-047: централизованно отключено администратором. Приоритет над
            # cfg.updates_enabled и ручной кнопкой — к GitHub не обращаемся.
            self._refresh_updates_policy_state()
            if manual:
                self.status_label.setText("Обновлениями управляет системный администратор.")
            return
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
            # US-019 (TASK-070): вместо простой блокировки предлагаем перейти на
            # облачную модель через overlay-пикер (локальная диктовка недоступна,
            # т.к. идёт локальная расшифровка файла).
            self._open_overlay_cloud_picker()
            return
        log.info("toggle_recording: starting recording")
        self.start_recording()

    def on_hotkey_press(self) -> None:
        # US-026 Push-to-Talk: нажатие комбинации — старт записи. Технические
        # блокировки и матричная проверка — как в toggle_recording.
        if self._mic_autodetect_running():
            self.status_label.setText("Идёт автонастройка микрофона. Диктовка временно недоступна.")
            return
        if self.transcribe_worker and self.transcribe_worker.isRunning():
            return
        if self.recorder.is_recording:
            return  # уже идёт запись (защита от повторного срабатывания при удержании)
        ok, reason = self._can_start_dictation()
        if not ok:
            log.info("on_hotkey_press BLOCKED by _can_start_dictation: %s", reason)
            self.status_label.setText(reason)
            self._open_overlay_cloud_picker()
            return
        log.info("on_hotkey_press: starting recording (PTT)")
        self.start_recording()

    def on_hotkey_release(self) -> None:
        # US-026 Push-to-Talk: отпускание комбинации — стоп и расшифровка.
        if self.recorder.is_recording:
            log.info("on_hotkey_release: stopping recording (PTT)")
            self.stop_recording()

    def start_recording(self) -> None:
        # TASK-083 (US-019): логирование точки входа в start_recording
        log.info("start_recording entered. selected_model=%s overlay_enabled=%s", self.cfg.selected_model, self.cfg.overlay_enabled)
        try:
            # US-035 FIX: останавливаем pending autosave (350мс debounce), чтобы
            # save_settings ниже точно был последним записывающим cfg перед
            # стартом записи. Иначе таймер мог выстрелить после нашего save
            # и перезаписать cfg промежуточным состоянием widget.
            try:
                if hasattr(self, "_settings_save_timer") and self._settings_save_timer.isActive():
                    self._settings_save_timer.stop()
            except Exception:  # noqa: BLE001
                pass
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
            self._dictation_stopped_by_lock = False  # US-069: признак этой диктовки
            self.stop_escape_watch()
            self.recorder = AudioRecorder(sample_rate=self.cfg.sample_rate, input_device_id=self.cfg.audio_input_device_id, meeting_compatibility=self.cfg.audio_meeting_compatibility)
            # Only the main Voice Input Local window should count as "own window".
            # The floating overlay is also a window in this process; after dragging
            # it, Windows can report the overlay as foreground. Treating that as
            # own_window suppressed the no-target-field transcript preview.
            try:
                self.recording_started_in_own_window = foreground_matches_window_handle(int(self.winId()))
            except Exception:
                self.recording_started_in_own_window = foreground_belongs_to_current_process()
            # US-067: окно-получатель для режима «на момент начала записи».
            self.recording_target_hwnd = foreground_window_handle()
            self.recorder.start()
            self.cancel_requested = False
            self.live_last_request_at = 0.0
            self.live_processed_until_seconds = 0.0
            self.live_total_text = ""
            self.live_had_insertions = False
            self.live_unavailable_notice_shown = False
            self.pending_final = None
            self.live_target_is_text_field = (focused_control_accepts_text() is True) and not self.recording_started_in_own_window
            self.start_escape_watch()
            self.toggle_btn.setText("Остановить запись")
            self.status_label.setText("Идёт запись… Esc отменит запись, горячая клавиша остановит и запустит расшифровку.")
            if self.cfg.overlay_enabled:
                self.overlay.reset_for_new_recording(live_enabled=self.cfg.live_transcription)
                self.overlay.show_recording(0.0, live_enabled=self.cfg.live_transcription)
            self._start_overlay_audio_level_updates()
            self._start_session_lock_watch()  # US-069
            self.update_recording_badge()
            log.info("Recording started. live_target_is_text_field=%s own_window=%s model=%s", self.live_target_is_text_field, self.recording_started_in_own_window, self.cfg.selected_model)
        except Exception as exc:  # noqa: BLE001
            self._stop_overlay_audio_level_updates()
            self._stop_session_lock_watch()  # US-069
            log.exception("Recording start failed")
            self.toggle_btn.setText("Начать запись")
            self.status_label.setText("Запись недоступна. Подробности записаны в логи.")
            if self.cfg.overlay_enabled:
                self.overlay.show_error("Ошибка", seconds=5)
            QMessageBox.critical(self, "Запись недоступна", str(exc))

    def stop_recording(self) -> None:
        self._stop_overlay_audio_level_updates()
        self._stop_session_lock_watch()  # US-069
        # US-069: при остановке по блокировке экрана модальные окна не
        # показываем — они повисли бы позади экрана блокировки и заморозили
        # приложение до разблокировки. Причина остаётся в статус-строке и логах.
        stopped_by_lock = bool(getattr(self, "_dictation_stopped_by_lock", False))
        try:
            wav_path, duration = self.recorder.stop_to_wav()
            if duration < 1.0:
                self._cleanup_wav(wav_path)
                self.toggle_btn.setText("Начать запись")
                self.stop_escape_watch()
                self.update_recording_badge()
                if self.cfg.overlay_enabled:
                    self.overlay.show_cancelled(seconds=3)
                self.status_label.setText("Запись слишком короткая. Скажите фразу дольше 1 секунды и попробуйте снова.")
                if not stopped_by_lock:
                    QMessageBox.information(self, "Запись", "Запись слишком короткая или речь не обнаружена. Попробуйте ещё раз.")
                return
        except Exception as exc:  # noqa: BLE001
            log.exception("Recording stop failed")
            if not stopped_by_lock:
                QMessageBox.critical(self, "Запись", str(exc))
            else:
                self.status_label.setText("Не удалось сохранить запись, остановленную при блокировке экрана. Подробности в логах.")
            self.toggle_btn.setText("Начать запись")
            self.stop_escape_watch()
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
        self._stop_dictation_progress()  # US-022
        # Special case: final transcript preview is visible because no target
        # text field was detected. Esc should close the preview, not delete the
        # already saved transcript.
        if self.result_preview_active:
            self.result_preview_active = False
            self.result_preview_text = ""
            if self.cfg.overlay_enabled:
                self.overlay.show_idle()
            self.stop_escape_watch()
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
        self._stop_overlay_audio_level_updates()
        self._stop_session_lock_watch()  # US-069
        self._dictation_stopped_by_lock = False
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
        self.stop_escape_watch()
        log.info("Current recording/transcription cancelled by user")

    def _begin_final_transcription(self, wav_path: Path, duration: float, cfg: AppConfig) -> None:
        self.pending_final = None
        # US-015/US-016: запоминаем исходно выбранную модель, чтобы в сообщении
        # о fallback показать что именно облако упало.
        self._last_requested_model = cfg.selected_model
        self._dictation_used_cloud = is_cloud_model_key(cfg.selected_model)
        # US-022: прогресс процента диктовки в overlay/статус-строке. Гейт 2с —
        # процент показываем только если расшифровка длится дольше 2 секунд
        # (короткие диктовки сразу отдают результат). Имеет смысл только для
        # локальных моделей; для облачных таймер не запускаем.
        self._dictation_progress_armed = False
        self._dictation_seg_floor = 0          # реальный прогресс по сегментам модели
        self._dictation_last_shown = -1        # троттлинг перерисовки overlay
        self._dictation_progress_t0 = 0.0      # момент включения анимации (monotonic)
        self._dictation_audio_duration = float(duration or 0.0)
        if getattr(self, "_dictation_progress_timer", None) is None:
            self._dictation_progress_timer = QTimer(self)
            self._dictation_progress_timer.setSingleShot(True)
            self._dictation_progress_timer.timeout.connect(self._arm_dictation_progress)
        if getattr(self, "_dictation_tick_timer", None) is None:
            self._dictation_tick_timer = QTimer(self)
            self._dictation_tick_timer.setInterval(250)
            self._dictation_tick_timer.timeout.connect(self._tick_dictation_progress)
        self._dictation_progress_timer.stop()
        self._dictation_tick_timer.stop()
        if not self._dictation_used_cloud:
            self._dictation_progress_timer.start(2000)
        self.transcribe_worker = TranscribeWorker(self.models, cfg.selected_model, wav_path, duration, cfg, is_live=False)
        self.transcribe_worker.finished_text.connect(lambda text, dur: self.on_transcription_done(text, dur, wav_path))
        self.transcribe_worker.fallback_applied.connect(self.on_cloud_fallback_applied)
        self.transcribe_worker.progress.connect(self.on_dictation_progress)
        self.transcribe_worker.no_speech.connect(lambda path=wav_path: self.on_dictation_no_speech(path))
        self.transcribe_worker.failed.connect(lambda detail, path=wav_path: self.on_transcription_failed(detail, path))
        self.transcribe_worker.start()

    def _arm_dictation_progress(self) -> None:
        # US-022: прошло 2 секунды — включаем показ процента и плавную анимацию.
        # Whisper для короткой диктовки выдаёт один сегмент в самом конце, поэтому
        # seg.end даёт прогресс лишь под занавес. Чтобы число двигалось, ведём
        # оценку по времени (тик-таймер), а реальные сегменты поднимают «пол».
        self._dictation_progress_armed = True
        self._dictation_progress_t0 = time.monotonic()
        t = getattr(self, "_dictation_tick_timer", None)
        if t is not None:
            t.start()
        self._tick_dictation_progress()

    def _dictation_progress_estimate(self) -> int:
        # US-022: монотонная оценка прогресса. Асимптотически приближается к 95%,
        # но не достигает 100% до фактического завершения. Реальный сегментный
        # прогресс модели задаёт нижнюю границу.
        t0 = getattr(self, "_dictation_progress_t0", 0.0) or 0.0
        floor = int(getattr(self, "_dictation_seg_floor", 0))
        if t0 <= 0:
            return min(95, floor)
        elapsed = max(0.0, time.monotonic() - t0)
        dur = float(getattr(self, "_dictation_audio_duration", 0.0) or 0.0)
        # Грубая оценка времени локального декода: ~0.6× длительности аудио,
        # но не меньше 3 секунд. tau управляет скоростью приближения к потолку.
        expected = max(3.0, dur * 0.6)
        tau = max(2.0, expected)
        frac = 1.0 - math.exp(-elapsed / tau)
        est = int(min(95.0, frac * 100.0))
        return max(est, min(95, floor))

    def _render_dictation_progress(self, pct: int) -> None:
        if self.cancel_requested or not getattr(self, "_dictation_progress_armed", False):
            return
        pct = max(0, min(100, int(pct)))
        # Троттлинг: не дёргаем overlay (raise_) при неизменном проценте.
        if pct == getattr(self, "_dictation_last_shown", -1):
            return
        self._dictation_last_shown = pct
        msg = f"Распознаю: {pct}%"
        self.status_label.setText(msg)
        # Диктовка приоритетна в overlay; открытый пикер модели не трогаем,
        # чтобы не схлопнуть список выбора (см. правило про «прилипчивый» пикер).
        if self.cfg.overlay_enabled and not self.overlay.is_in_picker():
            self.overlay.show_processing(msg)

    def _tick_dictation_progress(self) -> None:
        self._render_dictation_progress(self._dictation_progress_estimate())

    def on_dictation_progress(self, pct: int) -> None:
        # US-022: реальный сегментный прогресс модели поднимает «пол» оценки.
        if self.cancel_requested:
            return
        try:
            pct = max(0, min(100, int(pct)))
        except Exception:  # noqa: BLE001
            return
        self._dictation_seg_floor = max(int(getattr(self, "_dictation_seg_floor", 0)), pct)
        if getattr(self, "_dictation_progress_armed", False):
            self._render_dictation_progress(self._dictation_progress_estimate())

    def _stop_dictation_progress(self) -> None:
        # US-022: погасить гейт, тик-таймер и состояние процента (завершение/ошибка/отмена).
        self._dictation_progress_armed = False
        for _attr in ("_dictation_progress_timer", "_dictation_tick_timer"):
            t = getattr(self, _attr, None)
            if t is not None:
                t.stop()

    def on_cloud_fallback_applied(self, fallback_key: str, reason: str) -> None:
        """US-015/US-016: облачная модель упала → переключились на локальную.
        Перечитываем cfg (transcribe_with_fallback его сохранил), обновляем UI.
        BUG-CL-01: force_current=True гарантирует, что combo переключится на
        локальную модель, а не останется на cloud (которая всё ещё is_available)."""
        try:
            self.cfg = AppConfig.load()
        except Exception:  # noqa: BLE001
            pass
        self._dictation_used_cloud = False
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

    def on_dictation_no_speech(self, wav_path: Path) -> None:
        # EPIC-10/US-039: VAD не нашёл речи в облачной диктовке — облако не
        # вызывалось. Ничего не вставляем/копируем; overlay показывает статус.
        self._stop_dictation_progress()  # US-022
        self._cleanup_wav(wav_path)
        self.update_recording_badge()
        self.stop_escape_watch()
        self.toggle_btn.setEnabled(True)
        self.toggle_btn.setText("Начать запись")
        self.result_preview_active = False
        self.result_preview_text = ""
        if self.cancel_requested:
            self.status_label.setText("Расшифровка отменена.")
            return
        if self.cfg.overlay_enabled and not self.overlay.is_in_picker():
            self.overlay.show_no_speech()
        self.status_label.setText("Речь не найдена — ничего не вставлено.")

    def on_transcription_done(self, text: str, duration: float, wav_path: Path) -> None:
        self._stop_dictation_progress()  # US-022
        if self.cancel_requested:
            self._cleanup_wav(wav_path)
            self.update_recording_badge()
            self.stop_escape_watch()
            self.toggle_btn.setEnabled(True)
            self.status_label.setText("Расшифровка отменена. Результат проигнорирован.")
            return
        text = text.strip()
        # US-034: постобработка облачной диктовки через LLM (только cloud STT).
        if (
            text
            and getattr(self.cfg, "postprocess_enabled", False)
            and getattr(self, "_dictation_used_cloud", False)
            and (getattr(self.cfg, "postprocess_api_key", "") or "").strip()
        ):
            self._start_dictation_postprocess(text, duration, wav_path)
            return
        self._deliver_dictation_result(text, duration, wav_path)

    def _deliver_dictation_result(self, text: str, duration: float, wav_path: Path, *, postprocess_failed: bool = False) -> None:
        self.last_text.setPlainText(text)
        inserted = False
        should_try_paste = False
        show_overlay_result = False
        # US-069: диктовку, остановленную блокировкой экрана, НЕ вставляем в
        # активное окно. Расшифровка может закончиться, пока экран заблокирован,
        # а после разблокировки фокус окажется неизвестно где — текст улетел бы
        # в чужое поле. Всё остальное (буфер, история, плашка, постобработка)
        # работает как обычно. Признак одноразовый: снимаем его здесь.
        stopped_by_lock = bool(getattr(self, "_dictation_stopped_by_lock", False))
        self._dictation_stopped_by_lock = False
        if text:
            # US-070: решение о вставке принимается ЗДЕСЬ, в момент доставки
            # текста, а не по снимку состояния в начале записи. Во-первых, за
            # время диктовки пользователь мог поставить курсор в нужное поле —
            # раньше это не учитывалось вовсе. Во-вторых, наличие поля ввода
            # проверяет `copy_and_maybe_paste`, и только если пользователь
            # оставил включённой «Безопасную вставку»: прежний безусловный гейт
            # делал эту настройку недействующей для диктовки, из-за чего в
            # программах, рисующих поле самостоятельно (Chromium, Qt), вставку
            # нельзя было включить вообще ничем.
            should_try_paste = (
                self.cfg.auto_paste
                and not self.recording_started_in_own_window
                and not stopped_by_lock
            )
            if should_try_paste:
                # US-067: в режиме «на момент начала записи» вставка выполняется
                # только в то же окно; иначе текст остаётся в буфере и в плашке.
                expected_hwnd = (
                    self.recording_target_hwnd
                    if str(getattr(self.cfg, "paste_target_window", "on_finish")) == "on_start"
                    else None
                )
                inserted = copy_and_maybe_paste(
                    text,
                    auto_paste=True,
                    only_when_text_field_detected=self.cfg.paste_only_when_text_field_detected,
                    allow_current_process=False,
                    expected_foreground_hwnd=expected_hwnd,
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
        self.stop_escape_watch()
        if show_overlay_result and text and self.cfg.overlay_enabled:
            self.result_preview_active = True
            self.result_preview_text = text
            self.overlay.show_result_text(text)
            self.start_escape_watch()
            if stopped_by_lock:
                suffix = "экран был заблокирован; запись остановлена, текст показан под плашкой, сохранён в истории и скопирован в буфер"
            elif should_try_paste:
                suffix = "вставка не выполнена (поле ввода не найдено, окно сменилось или программа не приняла текст); текст показан под плашкой, сохранён в истории и скопирован в буфер"
            else:
                suffix = "поле ввода не найдено; текст показан под плашкой, сохранён в истории и скопирован в буфер"
        else:
            if self.cfg.overlay_enabled:
                QTimer.singleShot(1400, self.overlay.show_idle)
            if stopped_by_lock and text and not inserted:
                suffix = "экран был заблокирован; запись остановлена, текст сохранён в истории и скопирован в буфер"
            elif self.recording_started_in_own_window and text and not inserted:
                suffix = "текст показан во вкладке «Диктовка», сохранён в истории и скопирован в буфер"
            else:
                suffix = "текст вставлен" if inserted else "текст скопирован в буфер и сохранён в истории"
        if postprocess_failed:
            self.status_label.setText("Постобработка недоступна. Показан исходный текст расшифровки.")
        else:
            self.status_label.setText(f"Готово: {suffix}.")

    def _start_dictation_postprocess(self, text: str, duration: float, wav_path: Path) -> None:
        """US-034: запустить постобработку текста диктовки облачной LLM."""
        if self.cfg.overlay_enabled:
            self.overlay.show_processing("Улучшаю текст…")
        self.status_label.setText("Улучшаю текст через облачную LLM…")
        worker = PostProcessWorker(text, self.cfg)
        self._postprocess_worker = worker
        worker.finished_text.connect(
            lambda improved, d=duration, pth=wav_path: self._on_dictation_postprocess_done(improved, d, pth)
        )
        worker.failed.connect(
            lambda detail, raw=text, d=duration, pth=wav_path: self._on_dictation_postprocess_failed(detail, raw, d, pth)
        )
        worker.start()

    def _on_dictation_postprocess_done(self, improved: str, duration: float, wav_path: Path) -> None:
        text = (improved or "").strip()
        self._deliver_dictation_result(text, duration, wav_path)

    def _on_dictation_postprocess_failed(self, detail: str, raw_text: str, duration: float, wav_path: Path) -> None:
        first_line = (detail or "").splitlines()[0] if detail else ""
        log.warning("Dictation post-processing failed; delivering raw text. Detail: %s", first_line)
        self._deliver_dictation_result(raw_text, duration, wav_path, postprocess_failed=True)

    def on_transcription_failed(self, detail: str, wav_path: Path) -> None:
        self._stop_dictation_progress()  # US-022
        self._cleanup_wav(wav_path)
        if self.cancel_requested:
            log.info("Cancelled transcription failed after cancellation; suppressing user-facing error")
            self.update_recording_badge()
            self.stop_escape_watch()
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
        self.stop_escape_watch()
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

    def _start_session_lock_watch(self) -> None:
        """US-069: следить за блокировкой экрана, пока идёт запись."""
        try:
            self.session_lock_watcher.start(self._on_session_locked)
        except Exception:  # noqa: BLE001
            log.exception("Session lock watch start failed")

    def _stop_session_lock_watch(self) -> None:
        """US-069: снять наблюдение. Зовётся во всех точках завершения записи."""
        try:
            self.session_lock_watcher.stop()
        except Exception:  # noqa: BLE001
            pass

    def _on_session_locked(self) -> None:
        """US-069: экран заблокирован — остановить диктовку и расшифровать.

        Останавливаем ровно тем же путём, что и повторное нажатие горячей
        клавиши: запись прекращается, запускается расшифровка, текст попадает в
        историю и буфер обмена — надиктованное не теряется. В Push-to-Talk
        запись к этому моменту обычно уже остановлена отпусканием клавиш,
        поэтому проверка `is_recording` обязательна: второй остановки и второй
        расшифровки быть не должно. Расшифровка файла не затрагивается.
        """
        if not self.recorder.is_recording:
            self._stop_session_lock_watch()
            return
        log.info("Screen locked during dictation: stopping recording and transcribing")
        self._dictation_stopped_by_lock = True
        self.status_label.setText("Экран заблокирован. Диктовка остановлена, расшифровываю…")
        self.stop_recording()

    def _start_overlay_audio_level_updates(self) -> None:
        self.overlay.set_audio_level(0.0)
        if not self._audio_level_timer.isActive():
            self._audio_level_timer.start()

    def _stop_overlay_audio_level_updates(self) -> None:
        self._audio_level_timer.stop()
        self.overlay.set_audio_level(0.0)

    def _update_overlay_audio_level(self) -> None:
        if not self.recorder.is_recording:
            self._stop_overlay_audio_level_updates()
            return
        if self.cfg.overlay_enabled:
            self.overlay.set_audio_level(self.recorder.input_level)

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
        self.stop_escape_watch()
        self.status_label.setText("Текст скопирован. Готово.")

    def on_overlay_position_changed(self, x: int, y: int) -> None:
        self.cfg.overlay_x = int(x)
        self.cfg.overlay_y = int(y)
        self.cfg.save()

    # ── US-019: выбор облачной модели через overlay ──────────────────────

    def _cloud_models_for_picker(self) -> list[tuple[str, str]]:
        """Доступные облачные модели (key, label) для пикера US-019."""
        out: list[tuple[str, str]] = []
        for key in self.models.cloud_model_keys():
            if self.models.is_available(key):
                out.append((key, model_display_name(key)))
        return out

    def _all_models_for_picker(self) -> list[tuple[str, str]]:
        """Все доступные модели для быстрого выбора правым кликом по плашке."""
        out: list[tuple[str, str]] = []
        for key in self.models.available_model_keys():
            if not is_cloud_model_key(key):
                out.append((key, model_display_name(key)))
        for key in self.models.cloud_model_keys():
            if self.models.is_available(key):
                out.append((key, model_display_name(key)))
        return out

    def _open_overlay_cloud_picker(self) -> None:
        """TASK-070/073 (US-019): overlay-пикер облачных моделей при попытке
        локальной диктовки во время локальной расшифровки файла.

        Если облачные модели настроены — показываем их выбор; иначе — пустое
        состояние с кнопкой «Открыть настройки».
        """
        if not self.cfg.overlay_enabled:
            return
        self._overlay_picker_context = "parallel"
        cloud_models = self._cloud_models_for_picker()
        if cloud_models:
            self.overlay.show_model_picker(
                cloud_models,
                title="Идёт локальная расшифровка файла",
                current_key="",
                warning="Выберите облачную модель для параллельной диктовки. Данные уйдут провайдеру.",
            )
        else:
            self.overlay.show_model_picker(
                [],
                title="Идёт локальная расшифровка файла",
                show_settings_button=True,
                hint="Облачные модели не настроены. Добавьте API-ключ в настройках либо дождитесь завершения расшифровки файла.",
            )

    def on_overlay_picker_requested(self) -> None:
        """Правый клик по overlay в Ready открывает быстрый выбор модели."""
        if not self.cfg.overlay_enabled:
            return
        self._overlay_picker_context = "quick"
        models = self._all_models_for_picker()
        if models:
            self.overlay.show_model_picker(
                models,
                title="Быстрый выбор модели диктовки",
                current_key=str(self.cfg.selected_model or ""),
                warning="Облачные модели отправляют данные провайдеру.",
            )
        else:
            self.overlay.show_model_picker(
                [],
                title="Быстрый выбор модели диктовки",
                show_settings_button=True,
                hint="Нет доступных моделей. Загрузите модель во вкладке «Модели».",
            )

    def on_overlay_model_chosen(self, key: str) -> None:
        """TASK-071: применить выбор модели из overlay-пикера.

        Для облачных моделей — единый привратник безопасности US-018
        (_confirm_cloud_model_switch). При подтверждении: cfg.selected_model,
        save, синхронизация model_combo (force_current=True), overlay → Ready
        БЕЗ запуска записи.
        """
        key = str(key or "")
        if not key:
            self.overlay.show_idle()
            return
        # US-018: предупреждение/подтверждение при переходе на облако.
        if is_cloud_model_key(key):
            if not self._confirm_cloud_model_switch(key):
                self.overlay.show_idle()
                return
        if not self.models.is_available(key):
            self.status_label.setText("Выбранная модель недоступна. Выберите другую.")
            self.overlay.show_idle()
            return
        self.cfg.selected_model = key
        self.cfg.save()
        self.refresh_available_models_combo(force_current=True)
        self.overlay.show_idle()
        ctx = getattr(self, "_overlay_picker_context", "quick")
        if ctx == "parallel":
            self.status_label.setText(
                f"Выбрана модель «{model_display_name(key)}». Дважды щёлкните по плашке "
                f"или нажмите {self.cfg.hotkey}, чтобы начать диктовку."
            )
        else:
            self.status_label.setText(f"Модель диктовки: {model_display_name(key)}")
        log.info("Overlay model picker: selected %s (context=%s)", key, ctx)

    def on_overlay_settings_requested(self) -> None:
        """TASK-073 (US-019): из пустого состояния пикера открыть вкладку «Настройки»."""
        self.overlay.show_idle()
        try:
            self.showNormal()
            self.raise_()
            self.activateWindow()
            self.tabs.setCurrentIndex(3)  # 0 Диктовка,1 Файлы,2 Модели,3 Настройки,4 История
        except Exception:  # noqa: BLE001
            pass

    def on_overlay_picker_cancelled(self) -> None:
        """Escape закрывает пикер и возвращает корректное состояние плашки.

        При конфликте локальных задач возвращается прогресс файла; после
        быстрого выбора правым кликом — Ready. Модель не меняется.
        """
        ctx = getattr(self, "_overlay_picker_context", "quick")
        if ctx == "parallel" and self.is_file_busy():
            self.overlay.show_processing(getattr(self, "_last_file_overlay_text", "Файл…"))
            self.status_label.setText("Выбор облачной модели отменён. Идёт локальная расшифровка файла.")
        else:
            self.overlay.show_idle()
            self.status_label.setText("Выбор модели отменён.")
        log.info("Overlay picker cancelled (context=%s)", ctx)

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

    def export_history_item(self, fmt: str) -> None:
        """US-024: экспорт выбранной записи истории в TXT / MD / PDF."""
        current = self.history_list.currentItem()
        if not current:
            QMessageBox.information(self, "Скачать", "Сначала выберите запись в списке.")
            return
        item = current.data(Qt.UserRole)
        if not isinstance(item, HistoryItem) or not (item.text or "").strip():
            QMessageBox.information(self, "Скачать", "Выбранная запись не содержит текста.")
            return

        fmt = fmt.lower()
        filters = {
            "txt": "Текстовый файл (*.txt)",
            "md": "Markdown (*.md)",
            "pdf": "PDF (*.pdf)",
        }
        suggested = history_export.suggest_filename(item, fmt)
        target, _ = QFileDialog.getSaveFileName(
            self, "Сохранить расшифровку", suggested, filters.get(fmt, "Все файлы (*.*)")
        )
        if not target:
            return

        try:
            if fmt == "pdf":
                data = history_export.build_pdf(item)
                with open(target, "wb") as fh:
                    fh.write(data)
            else:
                content = history_export.build_md(item) if fmt == "md" else history_export.build_txt(item)
                with open(target, "w", encoding="utf-8") as fh:
                    fh.write(content)
        except ImportError:
            log.exception("PDF export failed: fpdf2 missing")
            QMessageBox.warning(
                self,
                "Скачать",
                "Для экспорта в PDF не установлена библиотека fpdf2.\n"
                "Установите её командой: pip install fpdf2",
            )
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("History export failed: %s", exc)
            QMessageBox.warning(self, "Скачать", f"Не удалось сохранить файл:\n{exc}")
            return

        self.status_label.setText(f"Расшифровка сохранена: {Path(target).name}")
        log.info("History item %s exported as %s → %s", item.id, fmt, target)

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
        """SUM-01 / US-036: суммаризация после расшифровки файла (локально/облако)."""
        if self.summarize_worker and self.summarize_worker.isRunning():
            return
        self.file_summary_label.setVisible(True)
        self.file_summary_text.setVisible(True)
        self.file_summary_text.setPlainText("Суммаризирую…")
        self.status_label.setText("Формирую краткое резюме…")
        self._dispatch_summary(text, kind="file")

    def _on_file_summary_done(self, summary: str) -> None:
        if summary.strip():
            self.file_summary_text.setPlainText(summary)
            self.status_label.setText("Расшифровка и суммаризация завершены.")
        else:
            self.file_summary_text.setPlainText("Суммаризация не дала результата.")
            self.status_label.setText("Файл расшифрован, но суммаризация пуста.")

    def summarize_history_item(self) -> None:
        """SUM-02 / US-036: суммаризация записи истории (локально/облако)."""
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
        self.summarize_history_btn.setEnabled(False)
        self.status_label.setText("Суммаризирую…")
        self._dispatch_summary(item.text, kind="history", item_id=item.id)

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

    def _dispatch_summary(self, text: str, *, kind: str, item_id=None) -> None:
        """US-036: запустить суммаризацию выбранным способом (local/cloud)."""
        self._summary_kind = kind
        self._summary_text = text
        self._summary_item_id = item_id
        mode = getattr(self.cfg, "summary_mode", "local") or "local"
        cloud_key = (getattr(self.cfg, "summary_api_key", "") or "").strip()
        if mode == "cloud" and cloud_key:
            self.status_label.setText("Облачная суммаризация…")
            worker = SummarizeWorker(
                text,
                system_prompt=getattr(self.cfg, "summary_system_prompt", "") or "",
                mode="cloud",
                cloud_api_key=cloud_key,
                cloud_base_url=getattr(self.cfg, "summary_base_url", "") or "https://api.openai.com/v1",
                cloud_model_id=getattr(self.cfg, "summary_model_id", "") or "",
                reasoning=bool(getattr(self.cfg, "summary_reasoning", False)),
                reasoning_effort=getattr(self.cfg, "summary_reasoning_effort", "low") or "low",
            )
            worker.finished_text.connect(self._on_summary_success)
            worker.cloud_failed.connect(self._on_cloud_summary_failed)
            self.summarize_worker = worker
            worker.start()
            return
        self._start_local_summary(text)

    def _start_local_summary(self, text: str) -> None:
        """US-036: локальная суммаризация (с проверкой наличия GGUF-модели)."""
        model_path = self._get_summary_model_path()
        if not model_path:
            if getattr(self, "_summary_kind", "") == "history":
                self.summarize_history_btn.setEnabled(True)
                QMessageBox.information(self, "Суммаризация", "Модель суммаризации не загружена. Загрузите её на вкладке Модели.")
            else:
                self.file_summary_label.setVisible(False)
                self.file_summary_text.setVisible(False)
                self.status_label.setText("Модель суммаризации не загружена. Загрузите её на вкладке Модели.")
            return
        if (getattr(self.cfg, "summary_mode", "local") or "local") == "local":
            self.status_label.setText(
                "Суммаризация выполняется локально — это займёт некоторое время. "
                "Для ускорения подключите облачную модель в Настройках → Суммаризация "
                "(текст передаётся на внешние серверы)."
            )
        worker = SummarizeWorker(
            text, model_path, getattr(self.cfg, "summary_system_prompt", "") or "",
            reasoning=bool(getattr(self.cfg, "summary_reasoning", False)),
            reasoning_effort=getattr(self.cfg, "summary_reasoning_effort", "low") or "low",
        )
        worker.finished_text.connect(self._on_summary_success)
        worker.failed.connect(self._on_summary_failed)
        self.summarize_worker = worker
        worker.start()

    def _on_summary_success(self, summary: str) -> None:
        """US-036: единая точка успеха — диспатч к file/history обработчику."""
        if getattr(self, "_summary_kind", "") == "history":
            self._on_history_summary_done(summary, getattr(self, "_summary_item_id", None))
        else:
            self._on_file_summary_done(summary)

    def _on_cloud_summary_failed(self, reason: str) -> None:
        """US-036: облако недоступно перед/во время суммаризации — выбор пользователя."""
        log.warning("Cloud summarization unavailable: %s", reason)
        text = getattr(self, "_summary_text", "") or ""
        if self._get_summary_model_path():
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle("Облачная суммаризация недоступна")
            box.setText("Облачная суммаризация недоступна.")
            box.setInformativeText(f"Причина: {reason}\n\nВыполнить суммаризацию локальной моделью или отменить?")
            local_btn = box.addButton("Выполнить локально", QMessageBox.AcceptRole)
            box.addButton("Отменить", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is local_btn:
                self._start_local_summary(text)
            else:
                self._cancel_summary_ui()
        else:
            QMessageBox.information(
                self, "Суммаризация невозможна",
                "Облачная суммаризация недоступна, а локальная модель не загружена.\n\n"
                "Пожалуйста, скачайте локальную модель суммаризации на вкладке «Модели».",
            )
            self._cancel_summary_ui()

    def _cancel_summary_ui(self) -> None:
        """US-036: вернуть UI в исходное состояние после отказа от суммаризации."""
        if getattr(self, "_summary_kind", "") == "history":
            self.summarize_history_btn.setEnabled(True)
        else:
            self.file_summary_label.setVisible(False)
            self.file_summary_text.setVisible(False)
        self.status_label.setText("Суммаризация отменена.")

    def _on_summary_mode_changed(self) -> None:
        """US-036: смена способа суммаризации. Переход на облако — привратник US-018."""
        mode = str(self.summary_mode_combo.currentData() or "local")
        if mode == "cloud" and not getattr(self, "_settings_loading", False):
            if not self._confirm_cloud_switch("summary"):
                self.summary_mode_combo.blockSignals(True)
                _idx = self.summary_mode_combo.findData("local")
                self.summary_mode_combo.setCurrentIndex(_idx if _idx >= 0 else 0)
                self.summary_mode_combo.blockSignals(False)
                self._update_summary_cloud_group_visibility()
                return
        self._update_summary_cloud_group_visibility()
        if not getattr(self, "_settings_loading", False):
            self.schedule_settings_autosave()

    def _update_summary_cloud_group_visibility(self) -> None:
        if hasattr(self, "summary_cloud_group"):
            mode = str(self.summary_mode_combo.currentData() or "local")
            self.summary_cloud_group.setVisible(mode == "cloud")

    def _on_summary_reasoning_toggled(self, checked: bool) -> None:
        """US-036: показать/скрыть выбор уровня рассуждения."""
        if hasattr(self, "summary_effort_row"):
            self.summary_effort_row.setVisible(bool(checked))

    def check_summary_connection(self) -> None:
        """US-036: «Проверить соединение» для облачной суммаризации."""
        api_key = self.summary_key_edit.text().strip()
        base_url = (self.summary_base_url_edit.text().strip() or "https://api.openai.com/v1")
        if not api_key:
            self.status_label.setText("Заполните API Key для облачной суммаризации.")
            return
        try:
            import requests  # noqa: F401
        except ImportError:
            self.status_label.setText("Не установлена библиотека requests. Выполните: .venv\\Scripts\\pip install requests")
            return
        self.save_settings(auto=True)
        self.summary_check_btn.setEnabled(False)
        self.summary_check_btn.setText("Проверяю…")
        self.status_label.setText("Проверка соединения с LLM-провайдером суммаризации…")
        worker = LlmConnectionCheckWorker(api_key, base_url)
        self._summary_check_worker = worker
        worker.result.connect(self._on_summary_check_done)
        worker.start()

    def _on_summary_check_done(self, ok: bool, message: str, models: list) -> None:
        self.summary_check_btn.setEnabled(True)
        self.summary_check_btn.setText("Проверить соединение и обновить список моделей")
        log.info("Summary LLM verify: ok=%s msg=%s models=%d", ok, message, len(models))
        if ok:
            combo = self.summary_model_combo
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
            if chosen and getattr(self.cfg, "summary_model_id", "") != chosen:
                self.cfg.summary_model_id = chosen
                self.cfg.save()
            self.status_label.setText(f"LLM суммаризации: {message} (моделей: {len(models)})")
        else:
            self.status_label.setText(f"LLM суммаризации: {message}")
            try:
                self.tray.showMessage("Voice Input Local", f"Суммаризация: {message}", QSystemTrayIcon.Warning, 5000)
            except Exception:  # noqa: BLE001
                pass

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
        """SUM-03 / US-036: сбросить промпт суммаризации к дефолтному значению."""
        self.summary_prompt_edit.setPlainText(DEFAULT_SUMMARY_SYSTEM_PROMPT)
        self.cfg.summary_system_prompt = DEFAULT_SUMMARY_SYSTEM_PROMPT
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
        # US-034: стартовая проверка LLM постобработки, если включено «Улучшение расшифровки».
        if getattr(self.cfg, "postprocess_enabled", False) and getattr(self.cfg, "postprocess_api_key", ""):
            wp = LlmConnectionCheckWorker(
                self.cfg.postprocess_api_key,
                self.cfg.postprocess_base_url or "https://api.openai.com/v1",
            )
            wp.result.connect(self._on_initial_postprocess_check_done)
            self._initial_cloud_check_workers.append(wp)
            wp.start()
            log.info("Initial cloud discover: started for postprocess LLM")
        # US-036: стартовая проверка LLM суммаризации (способ — облако и задан ключ).
        if (getattr(self.cfg, "summary_mode", "local") or "local") == "cloud" and getattr(self.cfg, "summary_api_key", ""):
            ws = LlmConnectionCheckWorker(
                self.cfg.summary_api_key,
                self.cfg.summary_base_url or "https://api.openai.com/v1",
            )
            ws.result.connect(self._on_initial_summary_check_done)
            self._initial_cloud_check_workers.append(ws)
            ws.start()
            log.info("Initial cloud discover: started for summary LLM")

    def _on_initial_postprocess_check_done(self, ok: bool, message: str, models: list) -> None:
        """US-034: результат стартовой проверки LLM постобработки. Тихо обновляет
        список моделей при успехе; при сбое — статус + трей-уведомление (постобработка
        при диктовке просто не сработает, доставится сырой текст расшифровки)."""
        log.info("Initial postprocess LLM check: ok=%s msg=%s models=%d", ok, message, len(models))
        if ok:
            if hasattr(self, "postprocess_model_combo"):
                combo = self.postprocess_model_combo
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
            return
        msg = f"Постобработка: LLM недоступна ({message}). При диктовке будет показан исходный текст расшифровки."
        self.status_label.setText(msg)
        try:
            self.tray.showMessage("Voice Input Local", msg, QSystemTrayIcon.Warning, 5000)
        except Exception:  # noqa: BLE001
            pass

    def _on_initial_summary_check_done(self, ok: bool, message: str, models: list) -> None:
        """US-036: результат стартовой проверки LLM суммаризации. Тихо обновляет
        список моделей при успехе; при сбое — статус + трей (в момент
        использования суммаризация предложит локальный fallback)."""
        log.info("Initial summary LLM check: ok=%s msg=%s models=%d", ok, message, len(models))
        if ok:
            if hasattr(self, "summary_model_combo"):
                combo = self.summary_model_combo
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
            return
        msg = f"Суммаризация: облачная LLM недоступна ({message}). Будет предложено выполнить локально."
        self.status_label.setText(msg)
        try:
            self.tray.showMessage("Voice Input Local", msg, QSystemTrayIcon.Warning, 5000)
        except Exception:  # noqa: BLE001
            pass

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

    def on_reset_postprocess_prompt(self) -> None:
        """US-034: сбросить системный промпт постобработки к дефолту."""
        if not hasattr(self, "postprocess_prompt_edit"):
            return
        self.postprocess_prompt_edit.setPlainText(DEFAULT_POSTPROCESS_SYSTEM_PROMPT)
        self.save_settings(auto=True)
        self.status_label.setText("Системный промпт постобработки сброшен к дефолту.")

    # ── US-044: вкладка «Словарь» — пользовательский словарь терминов ──────
    def _dictionary_tab(self) -> QWidget:
        """US-044/US-046: вкладка со словарём терминов для облачной постобработки.

        Три уровня, вложенные гейтами: постобработка → словарь → термин.
        - Мастер-чекбокс «Включить словарь» активен только при включённой
          постобработке (US-046).
        - Колонка «Вкл» (чекбокс у каждого термина) активна только при
          включённом словаре (US-046).
        Таблица только отображает записи; добавление/правка — через диалог
        TermDialog (двойной клик или кнопки), по образцу окна подключения.
        """
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        title = QLabel("Словарь терминов")
        title.setObjectName("Title")
        title.setStyleSheet("font-size: 16px;")
        layout.addWidget(title)

        subtitle = QLabel(
            "Специфические термины (бренды, имена, аббревиатуры, тех. лексика), которые "
            "распознавание часто коверкает. При включённой облачной постобработке ИИ "
            "исправляет их автоматически — с учётом контекста и правильной падежной формы. "
            "Двойной клик по строке — редактировать. Обязателен только «Термин». "
            "Колонка «Вкл» включает/отключает отдельные термины."
        )
        subtitle.setWordWrap(True)
        subtitle.setObjectName("Subtitle")
        layout.addWidget(subtitle)

        # US-046: мастер-тумблер словаря. Состояние ставим ДО подключения сигнала,
        # чтобы стартовый setChecked не дёргал автосохранение.
        self.postprocess_glossary_enabled_check = QCheckBox("Включить словарь")
        self.postprocess_glossary_enabled_check.setToolTip(
            "Применять словарь терминов при облачной постобработке. Доступно только когда включена постобработка."
        )
        self.postprocess_glossary_enabled_check.setChecked(
            bool(getattr(self.cfg, "postprocess_glossary_enabled", True))
        )
        self.postprocess_glossary_enabled_check.toggled.connect(self._on_glossary_enabled_toggled)
        layout.addWidget(self.postprocess_glossary_enabled_check)

        # Статус-строка, привязанная к состоянию постобработки/словаря.
        self.dictionary_status_label = QLabel("")
        self.dictionary_status_label.setWordWrap(True)
        layout.addWidget(self.dictionary_status_label)

        self.dictionary_table = QTableWidget(0, 5)
        self.dictionary_table.setHorizontalHeaderLabels(
            ["Вкл", "Термин", "Частые искажения", "Контекст/описание", "Исключения (когда НЕ заменять)"]
        )
        _dhh = self.dictionary_table.horizontalHeader()
        _dhh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        _dhh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        _dhh.setSectionResizeMode(2, QHeaderView.Stretch)
        _dhh.setSectionResizeMode(3, QHeaderView.Stretch)
        _dhh.setSectionResizeMode(4, QHeaderView.Stretch)
        self.dictionary_table.verticalHeader().setVisible(False)
        self.dictionary_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.dictionary_table.setSelectionMode(QTableWidget.SingleSelection)
        # Правка текста — только через диалог (не inline). Чекбокс «Вкл» работает
        # и при NoEditTriggers (клик по индикатору), сигнал — itemChanged.
        self.dictionary_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.dictionary_table.cellDoubleClicked.connect(self._on_dictionary_edit)
        self.dictionary_table.itemChanged.connect(self._on_dictionary_item_changed)
        layout.addWidget(self.dictionary_table, 1)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Добавить термин")
        add_btn.setFocusPolicy(Qt.NoFocus)  # TASK-047: Space (часть хоткея) не должен активировать кнопку
        add_btn.setAutoDefault(False)
        add_btn.clicked.connect(self._on_dictionary_add)
        edit_btn = QPushButton("Изменить")
        edit_btn.setFocusPolicy(Qt.NoFocus)
        edit_btn.setAutoDefault(False)
        edit_btn.clicked.connect(self._on_dictionary_edit)
        del_btn = QPushButton("Удалить выбранный")
        del_btn.setObjectName("Danger")
        del_btn.setFocusPolicy(Qt.NoFocus)
        del_btn.setAutoDefault(False)
        del_btn.clicked.connect(self._on_dictionary_delete_row)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(edit_btn)
        btn_row.addWidget(del_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        self._populate_dictionary_table()
        self._refresh_dictionary_tab_state()
        return tab

    def _populate_dictionary_table(self) -> None:
        """US-044: заполнить таблицу словаря из cfg.postprocess_glossary."""
        tbl = self.dictionary_table
        tbl.blockSignals(True)
        try:
            tbl.setRowCount(0)
            for entry in (getattr(self.cfg, "postprocess_glossary", None) or []):
                if not isinstance(entry, dict):
                    continue
                r = tbl.rowCount()
                tbl.insertRow(r)
                self._set_dictionary_row(r, entry)
        finally:
            tbl.blockSignals(False)

    def _set_dictionary_row(self, r: int, entry: dict) -> None:
        """US-044/US-046: записать запись словаря в строку. Колонка 0 — чекбокс
        «Вкл» (enabled, дефолт True); колонки 1..4 — текстовые поля."""
        tbl = self.dictionary_table
        chk = QTableWidgetItem()
        chk.setFlags(Qt.ItemIsSelectable | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
        chk.setCheckState(Qt.Checked if entry.get("enabled", True) else Qt.Unchecked)
        chk.setTextAlignment(Qt.AlignCenter)
        tbl.setItem(r, 0, chk)
        tbl.setItem(r, 1, QTableWidgetItem(str(entry.get("term", "") or "")))
        tbl.setItem(r, 2, QTableWidgetItem(str(entry.get("distortions", "") or "")))
        tbl.setItem(r, 3, QTableWidgetItem(str(entry.get("context", "") or "")))
        tbl.setItem(r, 4, QTableWidgetItem(str(entry.get("exclusions", "") or "")))

    def _dictionary_entry_from_row(self, r: int) -> dict:
        """US-044/US-046: прочитать запись словаря из строки (полный текст +
        флаг enabled из чекбокса колонки 0)."""
        tbl = self.dictionary_table
        def _cell(c: int) -> str:
            it = tbl.item(r, c)
            return it.text() if it is not None else ""
        chk = tbl.item(r, 0)
        enabled = True if chk is None else (chk.checkState() == Qt.Checked)
        return {
            "term": _cell(1),
            "distortions": _cell(2),
            "context": _cell(3),
            "exclusions": _cell(4),
            "enabled": enabled,
        }

    def _collect_dictionary_from_table(self) -> list:
        """US-044/US-046: собрать список записей словаря из таблицы. Строки без
        термина не сохраняются. enabled сохраняется как bool."""
        tbl = self.dictionary_table
        out: list[dict] = []
        for r in range(tbl.rowCount()):
            raw = self._dictionary_entry_from_row(r)
            term = (raw.get("term") or "").strip()
            if not term:
                continue
            out.append({
                "term": term,
                "distortions": (raw.get("distortions") or "").strip(),
                "context": (raw.get("context") or "").strip(),
                "exclusions": (raw.get("exclusions") or "").strip(),
                "enabled": bool(raw.get("enabled", True)),
            })
        return out

    def _on_dictionary_add(self) -> None:
        """US-044: добавить термин через диалог TermDialog (новый — включён)."""
        dlg = TermDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        entry = dlg.result_entry()
        entry["enabled"] = True
        tbl = self.dictionary_table
        r = tbl.rowCount()
        tbl.blockSignals(True)
        tbl.insertRow(r)
        self._set_dictionary_row(r, entry)
        tbl.blockSignals(False)
        tbl.setCurrentCell(r, 1)
        self._refresh_dictionary_tab_state()  # гейт чекбокса новой строки
        self.schedule_settings_autosave()

    def _on_dictionary_edit(self, *args) -> None:
        """US-044: редактировать выбранную запись словаря через TermDialog.

        Вызывается кнопкой «Изменить» и двойным кликом (cellDoubleClicked
        передаёт row/col). Двойной клик по колонке «Вкл» (0) диалог не открывает.
        Флаг enabled сохраняется (диалог его не трогает)."""
        if len(args) >= 2 and args[1] == 0:
            return
        r = self.dictionary_table.currentRow()
        if r < 0:
            return
        prev = self._dictionary_entry_from_row(r)
        dlg = TermDialog(self, entry=prev)
        if dlg.exec() != QDialog.Accepted:
            return
        entry = dlg.result_entry()
        entry["enabled"] = bool(prev.get("enabled", True))
        tbl = self.dictionary_table
        tbl.blockSignals(True)
        self._set_dictionary_row(r, entry)
        tbl.blockSignals(False)
        self._refresh_dictionary_tab_state()
        self.schedule_settings_autosave()

    def _on_dictionary_delete_row(self) -> None:
        """US-044: удалить выбранную строку словаря и сохранить."""
        tbl = self.dictionary_table
        r = tbl.currentRow()
        if r < 0:
            return
        tbl.removeRow(r)
        self.schedule_settings_autosave()

    def _on_dictionary_item_changed(self, *args) -> None:
        """US-046: пользователь переключил чекбокс «Вкл» термина → автосохранение.
        Программные изменения таблицы идут с blockSignals, сюда не попадают."""
        self.schedule_settings_autosave()

    def _on_glossary_enabled_toggled(self, checked: bool) -> None:
        """US-046: мастер-тумблер словаря → пере-гейт чекбоксов и автосохранение."""
        self._refresh_dictionary_tab_state()
        self.schedule_settings_autosave()

    def _set_dictionary_checkboxes_enabled(self, enabled: bool) -> None:
        """US-046: включить/заблокировать чекбоксы «Вкл» у всех терминов.
        Меняем только флаги колонки 0; сигналы блокируем, чтобы setFlags не
        триггерил itemChanged (ложное автосохранение)."""
        tbl = self.dictionary_table
        base = Qt.ItemIsSelectable
        flags = (base | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled) if enabled else base
        tbl.blockSignals(True)
        try:
            for r in range(tbl.rowCount()):
                it = tbl.item(r, 0)
                if it is not None:
                    it.setFlags(flags)
        finally:
            tbl.blockSignals(False)

    def _refresh_dictionary_tab_state(self) -> None:
        """US-044/US-046: отразить три состояния (постобработка → словарь →
        термин). Таблица редактируема всегда; гейтятся только мастер-чекбокс
        (по постобработке) и чекбоксы «Вкл» (по словарю)."""
        if not hasattr(self, "dictionary_table"):
            return
        pp_on = bool(
            getattr(self, "postprocess_enabled_check", None)
            and self.postprocess_enabled_check.isChecked()
        )
        master = getattr(self, "postprocess_glossary_enabled_check", None)
        # Мастер-чекбокс активен только при включённой постобработке (US-046).
        if master is not None:
            master.setEnabled(pp_on)
        gloss_on = pp_on and bool(master is not None and master.isChecked())
        # Чекбоксы «Вкл» терминов — только при включённом словаре (US-046).
        self._set_dictionary_checkboxes_enabled(gloss_on)
        if not pp_on:
            self.dictionary_status_label.setText(
                "Постобработка выключена — словарь недоступен. Термины можно добавлять впрок; "
                "включите постобработку на вкладке «Модели» → «Облачные модели»."
            )
            self.dictionary_status_label.setStyleSheet("color: #71717a;")
            self.dictionary_table.setStyleSheet("color: #71717a;")
        elif not gloss_on:
            self.dictionary_status_label.setText(
                "Словарь выключен — термины к постобработке не применяются. Включите чекбокс «Включить словарь» выше."
            )
            self.dictionary_status_label.setStyleSheet("color: #71717a;")
            self.dictionary_table.setStyleSheet("color: #71717a;")
        else:
            self.dictionary_status_label.setText(
                "Словарь применяется: включены постобработка и словарь. Отдельные термины — чекбоксом «Вкл»."
            )
            self.dictionary_status_label.setStyleSheet("color: #4ade80;")  # зелёный — активно
            self.dictionary_table.setStyleSheet("")

    def _on_postprocess_enabled_toggled(self, checked: bool) -> None:
        """US-034: показать/скрыть блок настроек постобработки."""
        # US-018: постобработка работает ТОЛЬКО через облако — при включении
        # показываем то же уведомление о передаче данных на внешние серверы.
        if checked and not getattr(self, "_settings_loading", False):
            if not self._confirm_cloud_switch("postprocess"):
                self.postprocess_enabled_check.blockSignals(True)
                self.postprocess_enabled_check.setChecked(False)
                self.postprocess_enabled_check.blockSignals(False)
                if hasattr(self, "postprocess_group"):
                    self.postprocess_group.setVisible(False)
                self._refresh_dictionary_tab_state()  # US-044: словарь снова не применяется
                return
        if hasattr(self, "postprocess_group"):
            self.postprocess_group.setVisible(bool(checked))
        self._refresh_dictionary_tab_state()  # US-044: обновить статус вкладки «Словарь»

    def _on_postprocess_reasoning_toggled(self, checked: bool) -> None:
        """US-034: показать/скрыть выбор уровня рассуждения."""
        if hasattr(self, "postprocess_effort_row"):
            self.postprocess_effort_row.setVisible(bool(checked))

    def check_llm_connection(self) -> None:
        """US-034: кнопка «Проверить соединение» для LLM-провайдера постобработки."""
        api_key = self.postprocess_key_edit.text().strip()
        base_url = (self.postprocess_base_url_edit.text().strip() or "https://api.openai.com/v1")
        if not api_key:
            self.status_label.setText("Заполните API Key для постобработки.")
            return
        try:
            import requests  # noqa: F401
        except ImportError:
            self.status_label.setText(
                "Не установлена библиотека requests. Выполните: .venv\\Scripts\\pip install requests"
            )
            return
        self.save_settings(auto=True)
        self.postprocess_check_btn.setEnabled(False)
        self.postprocess_check_btn.setText("Проверяю…")
        self.status_label.setText("Проверка соединения с LLM-провайдером постобработки…")
        worker = LlmConnectionCheckWorker(api_key, base_url)
        self._llm_check_worker = worker
        worker.result.connect(self._on_llm_check_done)
        worker.start()

    def _on_llm_check_done(self, ok: bool, message: str, models: list) -> None:
        self.postprocess_check_btn.setEnabled(True)
        self.postprocess_check_btn.setText("Проверить соединение и обновить список моделей")
        log.info("LLM verify result: ok=%s msg=%s models=%d", ok, message, len(models))
        if ok:
            combo = self.postprocess_model_combo
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
            if chosen and self.cfg.postprocess_model_id != chosen:
                self.cfg.postprocess_model_id = chosen
                self.cfg.save()
            self.status_label.setText(f"LLM постобработки: {message} (моделей: {len(models)})")
        else:
            self.status_label.setText(f"LLM постобработки: {message}")
            try:
                self.tray.showMessage("Voice Input Local", f"Постобработка: {message}", QSystemTrayIcon.Warning, 5000)
            except Exception:  # noqa: BLE001
                pass

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

    def _log_running_workers_on_quit(self) -> None:
        """DIAG (US-049/TASK-247): при выходе из трея фиксируем, какие фоновые
        QThread ещё выполняются, и состояние рекордера.

        Интермиттентный зомби-процесс + висящий оверлей при закрытии из трея:
        really_quit дожидается только 3 воркеров из ~14. Если в момент выхода
        жив недожидаемый (обычно сетевой) поток, уничтожение его работающего
        C++-объекта на финализации -> qFatal/abort может подвесить процесс.
        Лог помогает поймать конкретного виновника на реальном воспроизведении.
        Только чтение состояния — поведение выхода не меняется.
        """
        worker_attrs = (
            "transcribe_worker", "file_transcribe_worker", "preload_worker",
            "download_worker", "microphone_autodetect_worker", "summarize_worker",
            "update_check_worker", "update_download_worker", "live_worker",
            "_postprocess_worker", "_llm_check_worker", "_cloud_check_worker",
            "_check_worker", "_summary_check_worker",
        )
        running = []
        for _name in worker_attrs:
            _worker = getattr(self, _name, None)
            try:
                if _worker is not None and _worker.isRunning():
                    running.append(_name)
            except Exception:  # noqa: BLE001
                pass
        try:
            recording = bool(getattr(self, "recorder", None) and self.recorder.is_recording)
        except Exception:  # noqa: BLE001
            recording = False
        try:
            api_on = bool(getattr(self.cfg, "api_enabled", False))
        except Exception:  # noqa: BLE001
            api_on = False
        log.info(
            "really_quit: closing from tray. recorder_recording=%s api_enabled=%s running_workers=%s",
            recording, api_on, ", ".join(running) if running else "none",
        )

    def _shutdown_workers(self) -> None:
        """US-049: отменить и коротко (с общим бюджетом) подождать фоновые
        QThread-воркеры перед выходом. os._exit(0) в really_quit всё равно
        гарантирует завершение — этот метод лишь даёт воркерам шанс закрыть
        свои ресурсы (файлы/соединения). Ожидание ограничено ~3с суммарно,
        чтобы выход не подвисал.
        """
        worker_attrs = (
            "transcribe_worker", "file_transcribe_worker", "preload_worker",
            "download_worker", "microphone_autodetect_worker", "summarize_worker",
            "update_check_worker", "update_download_worker", "live_worker",
            "_postprocess_worker", "_llm_check_worker", "_cloud_check_worker",
            "_check_worker", "_summary_check_worker",
        )
        running = []
        for _name in worker_attrs:
            _worker = getattr(self, _name, None)
            if _worker is None:
                continue
            try:
                if not _worker.isRunning():
                    continue
            except Exception:  # noqa: BLE001
                continue
            _cancel = getattr(_worker, "cancel", None)
            if callable(_cancel):
                try:
                    _cancel()
                except Exception:  # noqa: BLE001
                    pass
            running.append(_worker)
        if not running:
            return
        deadline = time.monotonic() + 3.0
        for _worker in running:
            remaining_ms = int(max(0.0, deadline - time.monotonic()) * 1000)
            try:
                _worker.wait(remaining_ms if remaining_ms > 0 else 1)
            except Exception:  # noqa: BLE001
                pass

    def really_quit(self) -> None:
        # US-049: гарантированный ПОЛНЫЙ выход. QApplication.quit(), вызванный
        # из вложенного event loop контекстного меню трея, завершает ТОЛЬКО
        # вложенный цикл — главный app.exec() продолжает крутиться, приложение
        # остаётся «полуживым» (оверлей интерактивен, но hotkey уже снят →
        # диктовка не работает). Подтверждено логами: пикер моделей срабатывал
        # через 33с после quit(). Плюс при api_enabled uvicorn-потоки мешают
        # чистой финализации. Поэтому: аккуратно гасим фон, затем ПРИНУДИТЕЛЬНО
        # завершаем процесс через os._exit(0).
        if getattr(self, "_quitting", False):
            return
        self._quitting = True
        self._log_running_workers_on_quit()
        self.stop_escape_watch()
        self._stop_session_lock_watch()  # US-069
        # 1) Останавливаем ВСЕ периодические таймеры (иначе тик 300мс может
        #    заново показать оверлей / трогать UI во время разбора).
        try:
            for _val in list(self.__dict__.values()):
                if isinstance(_val, QTimer):
                    try:
                        _val.stop()
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            pass
        # 2) Снимаем системную регистрацию горячей клавиши и наблюдение
        #    за клавишами комбинации (Push-to-Talk).
        try:
            self.hotkey.stop()
        except Exception:  # noqa: BLE001
            pass
        # 3) Освобождаем аудиоустройство (сброс без сохранения WAV).
        try:
            if getattr(self, "recorder", None) and self.recorder.is_recording:
                self.recorder.cancel()
        except Exception:  # noqa: BLE001
            pass
        # 4) Отменяем и коротко ждём фоновые воркеры (best-effort).
        self._shutdown_workers()
        # 5) Убираем оверлей и трей с экрана.
        try:
            self.tray.hide()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.overlay.hide()
        except Exception:  # noqa: BLE001
            pass
        # 6) Сбрасываем буферы логов и ПРИНУДИТЕЛЬНО завершаем процесс.
        #    Нельзя полагаться на QApplication.quit()/возврат из app.exec().
        try:
            import logging as _logging
            _logging.shutdown()
        except Exception:  # noqa: BLE001
            pass
        os._exit(0)



def _single_instance_server_name() -> str:
    # Имя канала активации — per-user (именованные пайпы Windows машинно-глобальны,
    # несколько пользователей на одной машине не должны мешать друг другу).
    user = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
    return "VoiceInputLocal-activate-" + user


def _activate_running_instance(server_name: str, timeout_ms: int = 400) -> bool:
    """US-054: сигналит уже работающему экземпляру показать окно.

    True — сигнал доставлен (окно развернёт работающий экземпляр); False —
    канал недоступен, вызывающий покажет прежнее уведомление «уже запущено».
    """
    try:
        from PySide6.QtNetwork import QLocalSocket
    except Exception:  # noqa: BLE001
        return False
    # На Windows заранее отдаём право вывода окна вперёд любому процессу — этот,
    # только что запущенный из ярлыка, обычно ещё владеет foreground-правом.
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
        except Exception:  # noqa: BLE001
            pass
    socket = QLocalSocket()
    try:
        socket.connectToServer(server_name)
        if not socket.waitForConnected(timeout_ms):
            return False
        socket.write(b"SHOW\n")
        socket.flush()
        socket.waitForBytesWritten(timeout_ms)
        # Ждём ACK сервера — гарантия, что он принял сигнал (и показал окно),
        # прежде чем этот процесс завершится и закроет канал.
        socket.waitForReadyRead(timeout_ms)
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        try:
            socket.disconnectFromServer()
        except Exception:  # noqa: BLE001
            pass


def run() -> int:
    """Entry point: создаёт QApplication, главное окно и запускает event loop."""
    setup_logging()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    from PySide6.QtCore import QLockFile, QStandardPaths
    import tempfile as _tempfile
    _lock_dir = QStandardPaths.writableLocation(QStandardPaths.TempLocation) or _tempfile.gettempdir()
    _lock_path = str(Path(_lock_dir) / "VoiceInputLocal.lock")
    _server_name = _single_instance_server_name()
    _lock = QLockFile(_lock_path)
    _lock.setStaleLockTime(30000)
    if not _lock.tryLock(100):
        # US-054: экземпляр уже запущен — просим его показать окно, а не открываем
        # «вторую версию». Канал недоступен -> прежнее уведомление (fallback).
        log.info("Another VoiceInputLocal instance is running — requesting activation (server=%s).", _server_name)
        if _activate_running_instance(_server_name):
            log.info("Activation signal delivered to running instance; exiting silently.")
            return 0
        log.warning("Could not signal running instance (lock=%s) — showing 'already running' notice.", _lock_path)
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

    # US-054: слушаем канал активации, чтобы повторный запуск ярлыка/.exe
    # разворачивал ЭТО окно (второй процесс шлёт сигнал и выходит).
    try:
        from PySide6.QtNetwork import QLocalServer
        QLocalServer.removeServer(_server_name)  # снять устаревший канал (после крэша)
        _activation_server = QLocalServer()

        def _on_activation_request() -> None:
            sock = _activation_server.nextPendingConnection()
            if sock is not None:
                try:
                    if not sock.bytesAvailable():
                        sock.waitForReadyRead(500)
                    sock.readAll()
                    sock.write(b"OK\n")  # ACK: второй процесс дождётся доставки
                    sock.flush()
                    sock.waitForBytesWritten(200)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    sock.disconnectFromServer()
                except Exception:  # noqa: BLE001
                    pass
            log.info("Activation request received — showing window from tray.")
            window.show_from_tray()

        _activation_server.newConnection.connect(_on_activation_request)
        if _activation_server.listen(_server_name):
            app._voice_input_activation_server = _activation_server  # type: ignore[attr-defined]
        else:
            log.warning("Activation server listen failed (%s): %s", _server_name, _activation_server.errorString())
    except Exception:  # noqa: BLE001
        log.exception("Failed to start activation server (US-054); continuing without it.")

    return app.exec()
