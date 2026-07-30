from __future__ import annotations

import math

from PySide6.QtCore import QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class HotkeySafeComboBox(QComboBox):
    """QComboBox, который не открывает выпадающий список по клавише Space.

    Space — часть глобального хоткея (например Ctrl+Space). Без этого фокусный
    combo (на вкладке «Диктовка» или в overlay-пикере) перехватывал бы Space и
    самопроизвольно открывал список при нажатии хоткея. Остальные клавиши
    (стрелки, Enter) работают штатно.
    """

    def keyPressEvent(self, event) -> None:  # noqa: ANN001
        if event.key() == Qt.Key_Space:
            event.ignore()
            return
        super().keyPressEvent(event)


class RecordingOverlay(QWidget):
    """Always-on-top status pill.

    Normal states are intentionally tiny: a colored dot plus one word. The
    adaptive transcript preview is used only when there was no detected target
    text field for final insertion.
    """

    copy_requested = Signal(str)
    position_changed = Signal(int, int)
    # US-019: выбор облачной модели через overlay при конфликте локальных задач.
    model_selected = Signal(str)      # пользователь выбрал модель (ключ)
    settings_requested = Signal()     # из пустого состояния пикера — «Открыть настройки»
    picker_requested = Signal()       # правый клик по плашке в состоянии Ready
    picker_cancelled = Signal()       # Escape в режиме пикера
    toggle_recording_requested = Signal()  # двойной клик вне режима пикера

    COMPACT_MIN_WIDTH = 70
    COMPACT_MAX_WIDTH = 112
    COMPACT_HEIGHT = 26
    PREVIEW_MIN_WIDTH = 260
    PREVIEW_MAX_WIDTH = 560
    PREVIEW_MAX_HEIGHT = 240

    def __init__(self) -> None:
        # WA_ShowWithoutActivating защищает только момент показа окна. Отдельный
        # WindowDoesNotAcceptFocus не даёт обычному клику сделать overlay
        # foreground-окном и тем самым сломать автовставку в исходное поле.
        flags = (
            Qt.Tool
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.WindowDoesNotAcceptFocus
        )
        super().__init__(None, flags)
        try:
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        except AttributeError:
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setWindowTitle("Индикатор Voice Input Local")
        self._drag_start: QPoint | None = None
        self._hotkey = "Ctrl+Alt+Space"
        self._result_text = ""
        # _idle управляет только быстрым выбором модели по правому клику.
        # Политику старта/остановки записи определяет MainWindow.toggle_recording,
        # а не визуальное состояние overlay. _in_picker=True пока показан выбор
        # модели: на это время окно снова принимает фокус для popup QComboBox.
        self._idle = False
        self._in_picker = False
        self._return_timer = QTimer(self)
        self._return_timer.setSingleShot(True)
        self._return_timer.timeout.connect(self.show_idle)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.card = QFrame()
        self.card.setObjectName("OverlayCard")
        self.card.setStyleSheet(
            """
            QFrame#OverlayCard {
                background: rgba(18, 18, 22, 238);
                border: none;
                border-radius: 12px;
            }
            QLabel { color: #f4f4f5; font-size: 10px; font-weight: 650; }
            QLabel#Dot { color: #22c55e; font-size: 10px; font-weight: 900; }
            QLabel#Preview {
                background: rgba(39, 39, 42, 232);
                border: none;
                border-radius: 10px;
                padding: 8px;
                font-size: 12px;
                font-weight: 500;
                line-height: 140%;
            }
            QPushButton {
                background: rgba(244, 244, 245, 235);
                color: #111113;
                border: none;
                border-radius: 8px;
                padding: 6px 9px;
                font-size: 11px;
                font-weight: 700;
            }
            QPushButton:hover { background: #ffffff; }
            QPushButton:pressed { background: #d4d4d8; padding-top: 7px; padding-bottom: 5px; }
            QLabel#PickerInfo { color: #f4f4f5; font-size: 11px; font-weight: 600; }
            QLabel#PickerHint { color: #fbbf24; font-size: 10px; font-weight: 600; }
            QComboBox {
                background: rgba(39, 39, 42, 240);
                color: #f4f4f5;
                border: 1px solid #3f3f46;
                border-radius: 8px;
                padding: 5px 8px;
                font-size: 12px;
                font-weight: 600;
            }
            QComboBox:hover { border-color: #52525b; }
            QComboBox QAbstractItemView {
                background: #18181b;
                color: #f4f4f5;
                selection-background-color: #2563eb;
                border: 1px solid #3f3f46;
                outline: none;
            }
            """
        )
        self.card_layout = QVBoxLayout(self.card)
        self.card_layout.setContentsMargins(7, 4, 7, 4)
        self.card_layout.setSpacing(6)

        self.top_row = QHBoxLayout()
        self.top_row.setSpacing(4)
        self.dot_label = QLabel("●")
        self.dot_label.setObjectName("Dot")
        self.status_label = QLabel("Ready")
        self.top_row.addWidget(self.dot_label)
        self.top_row.addWidget(self.status_label, 1)
        self.card_layout.addLayout(self.top_row)

        self._interaction_tooltip = (
            "Двойной левый клик — начать или остановить запись\n"
            "Правый клик — выбрать модель"
        )
        for widget in (self, self.card, self.dot_label, self.status_label):
            widget.setToolTip(self._interaction_tooltip)

        self.preview_label = QLabel("")
        self.preview_label.setObjectName("Preview")
        self.preview_label.setWordWrap(True)
        self.preview_label.setMinimumWidth(self.PREVIEW_MIN_WIDTH)
        self.preview_label.setMaximumWidth(self.PREVIEW_MAX_WIDTH)
        self.preview_label.setVisible(False)
        self.card_layout.addWidget(self.preview_label)

        self.copy_btn = QPushButton("Скопировать")
        self.copy_btn.setVisible(False)
        self.copy_btn.clicked.connect(self._copy_clicked)
        self.card_layout.addWidget(self.copy_btn)

        # US-019: виджеты режима выбора облачной модели (по умолчанию скрыты).
        self.picker_info = QLabel("")
        self.picker_info.setObjectName("PickerInfo")
        self.picker_info.setWordWrap(True)
        self.picker_info.setMinimumWidth(self.PREVIEW_MIN_WIDTH)
        self.picker_info.setMaximumWidth(self.PREVIEW_MAX_WIDTH)
        self.picker_info.setVisible(False)
        self.card_layout.addWidget(self.picker_info)

        self.picker_combo = HotkeySafeComboBox()
        self.picker_combo.setVisible(False)
        self.picker_combo.activated.connect(self._picker_combo_activated)
        self.card_layout.addWidget(self.picker_combo)

        self.picker_settings_btn = QPushButton("Открыть настройки")
        self.picker_settings_btn.setVisible(False)
        self.picker_settings_btn.clicked.connect(self._settings_clicked)
        self.card_layout.addWidget(self.picker_settings_btn)

        self.picker_hint = QLabel("")
        self.picker_hint.setObjectName("PickerHint")
        self.picker_hint.setWordWrap(True)
        self.picker_hint.setMinimumWidth(self.PREVIEW_MIN_WIDTH)
        self.picker_hint.setMaximumWidth(self.PREVIEW_MAX_WIDTH)
        self.picker_hint.setVisible(False)
        self.card_layout.addWidget(self.picker_hint)

        outer.addWidget(self.card)
        self._place_default()
        self.show_idle()

    def _place_default(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            self.move(40, 40)
            return
        geo = screen.availableGeometry()
        self._resize_to_content(compact=True)
        self.move(geo.right() - self.width() - 32, geo.top() + 32)
    def restore_position(self, x: int | None, y: int | None) -> None:
        """Restore the saved overlay position, clamping it to a visible screen."""
        if x is None or y is None:
            return
        try:
            point = QPoint(int(x), int(y))
        except Exception:
            return
        self.move(self._clamp_point_to_screens(point))

    def _clamp_point_to_screens(self, point: QPoint) -> QPoint:
        screens = QApplication.screens()
        if not screens:
            return point
        margin = 8
        size = self.size() if self.size().isValid() else self.sizeHint()
        width = max(size.width(), self.COMPACT_MIN_WIDTH)
        height = max(size.height(), self.COMPACT_HEIGHT)

        target_geo = None
        for screen in screens:
            geo = screen.availableGeometry()
            if geo.contains(point):
                target_geo = geo
                break
        if target_geo is None:
            primary = QApplication.primaryScreen()
            target_geo = (primary or screens[0]).availableGeometry()

        min_x = target_geo.left() + margin
        min_y = target_geo.top() + margin
        max_x = target_geo.right() - width - margin
        max_y = target_geo.bottom() - height - margin
        if max_x < min_x:
            max_x = min_x
        if max_y < min_y:
            max_y = min_y
        return QPoint(min(max(point.x(), min_x), max_x), min(max(point.y(), min_y), max_y))

    def _ensure_visible_on_screen(self) -> None:
        self.move(self._clamp_point_to_screens(self.pos()))


    def _resize_to_content(self, *, compact: bool = False) -> None:
        if compact:
            self.preview_label.setVisible(False)
            self.copy_btn.setVisible(False)
            self.card.setMinimumWidth(self.COMPACT_MIN_WIDTH)
            self.card.setMaximumWidth(self.COMPACT_MAX_WIDTH)
            self.adjustSize()
            width = min(max(self.sizeHint().width(), self.COMPACT_MIN_WIDTH), self.COMPACT_MAX_WIDTH)
            self.resize(QSize(width, self.COMPACT_HEIGHT))
            return

        self.card.setMinimumWidth(self.PREVIEW_MIN_WIDTH)
        self.card.setMaximumWidth(self.PREVIEW_MAX_WIDTH + 24)
        self.card.updateGeometry()
        self.preview_label.updateGeometry()
        self.adjustSize()
        hint = self.sizeHint()
        width = min(max(hint.width(), self.PREVIEW_MIN_WIDTH), self.PREVIEW_MAX_WIDTH + 24)
        height = max(self.COMPACT_HEIGHT, hint.height())
        self.resize(QSize(width, height))

    def _set_state(self, status: str, dot_color: str, *, compact: bool, auto_ready_ms: int | None = None) -> None:
        self._return_timer.stop()
        # Любой обычный статус выводит плашку из режима пикера.
        if self._in_picker:
            self._exit_picker_mode()
        self._idle = False
        self._hide_picker_widgets()
        self.status_label.setText(status)
        self.dot_label.setText("●")
        self.dot_label.setStyleSheet(f"color: {dot_color};")
        if compact:
            self._result_text = ""
            self.preview_label.setText("")
            self.preview_label.setMinimumHeight(0)
            self.preview_label.setVisible(False)
            self.copy_btn.setVisible(False)
        if not self.isVisible():
            self.show()
        self._resize_to_content(compact=compact)
        self._ensure_visible_on_screen()
        self.raise_()
        if auto_ready_ms is not None:
            self._return_timer.start(auto_ready_ms)

    def set_hotkey(self, hotkey: str) -> None:
        self._hotkey = hotkey or "Ctrl+Alt+Space"

    def show_idle(self, message: str = "Ready") -> None:
        self._set_state(message, "#22c55e", compact=True)
        # Из Ready доступен быстрый выбор модели по правому клику.
        self._idle = True

    def hide_overlay(self) -> None:
        self._return_timer.stop()
        self._result_text = ""
        self.preview_label.setText("")
        self.preview_label.setVisible(False)
        self.copy_btn.setVisible(False)
        if self._in_picker:
            self._exit_picker_mode()
        self._hide_picker_widgets()
        self._idle = False
        self.hide()

    def reset_for_new_recording(self, *, live_enabled: bool = False) -> None:
        self._return_timer.stop()
        self._result_text = ""
        self.preview_label.setText("")
        self.preview_label.setMinimumHeight(0)
        self.preview_label.setMaximumHeight(self.PREVIEW_MAX_HEIGHT)
        self.preview_label.setVisible(False)
        self.copy_btn.setVisible(False)
        self.show_recording(0.0, live_enabled=live_enabled)

    def show_recording(self, elapsed: float = 0.0, *, live_enabled: bool = False) -> None:
        # Live is disabled in the stable build, so recording remains compact.
        self._set_state("Запись", "#ef4444", compact=True)

    def show_processing(self, label: str = "Распознаю") -> None:
        self._set_state(label, "#f59e0b", compact=True)

    def show_cancelled(self, seconds: int = 4) -> None:
        self._set_state("Отменено", "#a1a1aa", compact=True, auto_ready_ms=max(1, seconds) * 1000)

    def show_no_speech(self, seconds: int = 4) -> None:
        # EPIC-10/US-039: VAD не нашёл речи в облачной диктовке — это НЕ ошибка,
        # поэтому нейтральный серый статус с авто-возвратом в Ready.
        self._set_state("Речь не найдена", "#a1a1aa", compact=True, auto_ready_ms=max(1, seconds) * 1000)

    def show_error(self, message: str = "Ошибка", seconds: int = 5) -> None:
        self._set_state(message, "#ef4444", compact=True, auto_ready_ms=max(1, seconds) * 1000)

    def set_idle(self) -> None:
        self.show_idle()

    def show_result_text(self, text: str) -> None:
        """Show final transcript when there was no target text field."""
        self._return_timer.stop()
        self._idle = False
        self._result_text = text.strip()
        if not self._result_text:
            self.show_idle()
            return
        clipped = self._result_text[-1600:]
        line_count = max(1, math.ceil(len(clipped) / 58) + clipped.count("\n"))
        target_height = min(self.PREVIEW_MAX_HEIGHT, max(44, 20 * line_count + 20))
        self.status_label.setText("Готово")
        self.dot_label.setStyleSheet("color: #22c55e;")
        self.preview_label.setText(clipped)
        self.preview_label.setMinimumHeight(min(target_height, self.PREVIEW_MAX_HEIGHT))
        self.preview_label.setMaximumHeight(self.PREVIEW_MAX_HEIGHT)
        self.preview_label.setVisible(True)
        self.copy_btn.setVisible(True)
        if not self.isVisible():
            self.show()
        self._resize_to_content(compact=False)
        self._ensure_visible_on_screen()
        self.raise_()

    def set_preview(self, text: str) -> None:
        self.show_result_text(text)

    def _copy_clicked(self) -> None:
        if self._result_text:
            self.copy_requested.emit(self._result_text)

    # ── US-019: режим выбора облачной модели ──────────────────────────────

    def _set_show_without_activating(self, value: bool) -> None:
        try:
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, value)
        except AttributeError:
            self.setAttribute(Qt.WA_ShowWithoutActivating, value)

    def _set_accepts_focus(self, value: bool) -> None:
        """Разрешить фокус только интерактивному пикеру моделей."""
        try:
            flag = Qt.WindowType.WindowDoesNotAcceptFocus
        except AttributeError:
            flag = Qt.WindowDoesNotAcceptFocus
        self.setWindowFlag(flag, not value)

    def _hide_picker_widgets(self) -> None:
        self.picker_info.setVisible(False)
        self.picker_combo.setVisible(False)
        self.picker_settings_btn.setVisible(False)
        self.picker_hint.setVisible(False)

    def _exit_picker_mode(self) -> None:
        """Выйти из режима пикера и вернуть non-activating поведение плашки.

        Оба ограничения фокуса восстанавливаются обязательно — иначе обычный
        клик по overlay перехватит foreground и сломает вставку текста в
        исходное активное окно.
        """
        self._in_picker = False
        self._set_accepts_focus(False)
        self._set_show_without_activating(True)
        self._hide_picker_widgets()

    def show_model_picker(
        self,
        models: list[tuple[str, str]],
        *,
        title: str = "Выбор модели",
        current_key: str = "",
        warning: str = "",
        show_settings_button: bool = False,
        hint: str = "",
    ) -> None:
        """Показать выбор модели прямо в плашке overlay.

        models — список (key, label). Если он пуст или show_settings_button=True,
        показывается пустое состояние с кнопкой «Открыть настройки» и подсказкой.
        Выбор модели через QComboBox без отдельной кнопки подтверждения —
        сигнал model_selected эмитится по activated.
        """
        self._return_timer.stop()
        self._idle = False
        self._in_picker = True
        self._result_text = ""
        self.preview_label.setText("")
        self.preview_label.setVisible(False)
        self.copy_btn.setVisible(False)

        self.status_label.setText("Выбор модели")
        self.dot_label.setText("●")
        self.dot_label.setStyleSheet("color: #38bdf8;")

        self.picker_info.setText(title)
        self.picker_info.setVisible(bool(title))

        empty = show_settings_button or not models
        if empty:
            self.picker_combo.setVisible(False)
            self.picker_settings_btn.setVisible(True)
            self.picker_hint.setText(hint or "Либо дождитесь завершения расшифровки файла")
            self.picker_hint.setVisible(True)
        else:
            self.picker_combo.blockSignals(True)
            self.picker_combo.clear()
            for key, label in models:
                self.picker_combo.addItem(label, key)
            if current_key:
                idx = self.picker_combo.findData(current_key)
                if idx >= 0:
                    self.picker_combo.setCurrentIndex(idx)
            self.picker_combo.blockSignals(False)
            self.picker_combo.setVisible(True)
            self.picker_settings_btn.setVisible(False)
            if warning:
                self.picker_hint.setText(warning)
                self.picker_hint.setVisible(True)
            else:
                self.picker_hint.setVisible(False)

        # На время пикера разрешаем плашке активироваться и принимать фокус,
        # иначе popup combo не сможет обрабатывать клавиатуру и выбор модели.
        self._set_accepts_focus(True)
        self._set_show_without_activating(False)
        if not self.isVisible():
            self.show()
        self._resize_to_content(compact=False)
        self._ensure_visible_on_screen()
        self.raise_()
        self.activateWindow()
        if self.picker_combo.isVisible():
            self.picker_combo.setFocus()

    def _picker_combo_activated(self, index: int) -> None:
        key = self.picker_combo.itemData(index)
        if key:
            self.model_selected.emit(str(key))

    def _settings_clicked(self) -> None:
        self.settings_requested.emit()

    def is_in_picker(self) -> bool:
        return self._in_picker

    def keyPressEvent(self, event) -> None:  # noqa: ANN001
        # US-019: Escape в режиме пикера закрывает список; в какое
        # состояние вернуть overlay (Ready или прогресс файла) решает MainWindow
        # по контексту пикера через сигнал picker_cancelled.
        if self._in_picker and event.key() == Qt.Key_Escape:
            self.picker_cancelled.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: ANN001
        if event.button() == Qt.RightButton:
            self._drag_start = None
            if self._idle and not self._in_picker:
                self.picker_requested.emit()
            event.accept()
            return
        if event.button() == Qt.LeftButton:
            self._drag_start = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001
        if self._drag_start is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_start)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001
        self._drag_start = None
        if event.button() == Qt.RightButton:
            event.accept()
            return
        self._ensure_visible_on_screen()
        self.position_changed.emit(self.pos().x(), self.pos().y())
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: ANN001
        # Overlay сообщает только о намерении пользователя. Реальные состояния,
        # параллельные задачи и технические блокировки проверяет общий обработчик
        # MainWindow.toggle_recording. В пикере двойной клик принадлежит его UI.
        if event.button() == Qt.LeftButton and not self._in_picker:
            self._drag_start = None
            self.toggle_recording_requested.emit()
            event.accept()
            return
        event.accept()
