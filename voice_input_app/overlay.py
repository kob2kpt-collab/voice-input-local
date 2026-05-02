from __future__ import annotations

import math

from PySide6.QtCore import QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget


class RecordingOverlay(QWidget):
    """Always-on-top status pill.

    Normal states are intentionally tiny: a colored dot plus one word. The
    adaptive transcript preview is used only when there was no detected target
    text field for final insertion.
    """

    copy_requested = Signal(str)
    position_changed = Signal(int, int)

    COMPACT_MIN_WIDTH = 70
    COMPACT_MAX_WIDTH = 112
    COMPACT_HEIGHT = 26
    PREVIEW_MIN_WIDTH = 260
    PREVIEW_MAX_WIDTH = 560
    PREVIEW_MAX_HEIGHT = 240

    def __init__(self) -> None:
        flags = Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
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

    def hide_overlay(self) -> None:
        self._return_timer.stop()
        self._result_text = ""
        self.preview_label.setText("")
        self.preview_label.setVisible(False)
        self.copy_btn.setVisible(False)
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

    def show_error(self, message: str = "Ошибка", seconds: int = 5) -> None:
        self._set_state(message, "#ef4444", compact=True, auto_ready_ms=max(1, seconds) * 1000)

    def set_idle(self) -> None:
        self.show_idle()

    def show_result_text(self, text: str) -> None:
        """Show final transcript when there was no target text field."""
        self._return_timer.stop()
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

    def mousePressEvent(self, event) -> None:  # noqa: ANN001
        if event.button() == Qt.LeftButton:
            self._drag_start = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001
        if self._drag_start is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_start)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001
        self._drag_start = None
        self._ensure_visible_on_screen()
        self.position_changed.emit(self.pos().x(), self.pos().y())
        event.accept()
