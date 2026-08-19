from __future__ import annotations

import math
from dataclasses import dataclass

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter
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


# US-077: выбор монитора для плашки
#
# Позицию плашки нельзя ограничивать «основным» монитором. Прежняя версия
# искала экран через availableGeometry().contains(левый верхний угол плашки)
# и при промахе откатывалась на primaryScreen. Промах — обычное дело: полоса
# панели задач, стык мониторов разной высоты, монитор правее или ниже
# основного. Из-за этого плашка при перетаскивании к стыку прыгала на
# основной монитор (отчёт владельца продукта).
#
# Целевой монитор выбирается в таком порядке:
#   1. идёт перетаскивание — монитор ПОД КУРСОРОМ, по ПОЛНОЙ геометрии;
#      именно полной, иначе курсор над панелью задач не попал бы никуда и
#      мы вернулись бы к тому же дефекту;
#   2. иначе — монитор с наибольшим пересечением с прямоугольником плашки:
#      плашка «принадлежит» тому экрану, где её видно больше всего;
#   3. пересечений нет вовсе (монитор отключили) — БЛИЖАЙШИЙ монитор,
#      а не основной.
#
# Логика вынесена в модульные функции над снимками ScreenInfo, потому что
# реальные мониторы в headless-тестах не эмулируются, а проверять правила
# нужно (tests/test_us077_overlay_monitors.py).

SCREEN_MARGIN = 8


@dataclass(frozen=True)
class ScreenInfo:
    """Снимок монитора: имя и геометрия.

    name — QScreen.name(): к нему привязывается сохранённая позиция плашки.
    geometry — полная геометрия монитора (вместе с панелью задач).
    available — рабочая область: в неё плашка втаскивается целиком.
    """

    name: str
    geometry: QRect
    available: QRect


def screen_infos(screens) -> list[ScreenInfo]:
    """Собрать снимки мониторов из QScreen-объектов (или готовых ScreenInfo)."""
    infos: list[ScreenInfo] = []
    for screen in screens or []:
        if isinstance(screen, ScreenInfo):
            infos.append(screen)
            continue
        try:
            geometry = QRect(screen.geometry())
            available = QRect(screen.availableGeometry())
            name = str(screen.name() or "")
        except Exception:  # noqa: BLE001
            continue
        if not available.isValid():
            available = geometry
        infos.append(ScreenInfo(name=name, geometry=geometry, available=available))
    return infos


def _axis_gap(low: int, high: int, value: int) -> int:
    if value < low:
        return low - value
    if value > high:
        return value - high
    return 0


def _distance_to_rect(rect: QRect, point: QPoint) -> int:
    """Квадрат расстояния от точки до прямоугольника (0 — точка внутри)."""
    dx = _axis_gap(rect.left(), rect.right(), point.x())
    dy = _axis_gap(rect.top(), rect.bottom(), point.y())
    return dx * dx + dy * dy


def pick_target_screen(
    infos: list[ScreenInfo],
    rect: QRect,
    cursor: QPoint | None = None,
) -> ScreenInfo | None:
    """Монитор, границы которого применяются к плашке (см. комментарий выше)."""
    if not infos:
        return None
    if cursor is not None:
        for info in infos:
            if info.geometry.contains(cursor):
                return info
        return min(infos, key=lambda i: _distance_to_rect(i.geometry, cursor))

    best: ScreenInfo | None = None
    best_area = 0
    for info in infos:
        overlap = info.geometry.intersected(rect)
        area = max(0, overlap.width()) * max(0, overlap.height())
        if area > best_area:
            best_area = area
            best = info
    if best is not None:
        return best
    return min(infos, key=lambda i: _distance_to_rect(i.geometry, rect.center()))


def clamp_rect_into(rect: QRect, area: QRect, margin: int = SCREEN_MARGIN) -> QPoint:
    """Втащить прямоугольник целиком в рабочую область монитора."""
    min_x = area.x() + margin
    min_y = area.y() + margin
    max_x = area.x() + area.width() - rect.width() - margin
    max_y = area.y() + area.height() - rect.height() - margin
    if max_x < min_x:
        max_x = min_x
    if max_y < min_y:
        max_y = min_y
    return QPoint(min(max(rect.x(), min_x), max_x), min(max(rect.y(), min_y), max_y))


def clamp_overlay_rect(
    infos: list[ScreenInfo],
    rect: QRect,
    cursor: QPoint | None = None,
    margin: int = SCREEN_MARGIN,
) -> "tuple[QPoint, ScreenInfo | None]":
    """Видимая целиком позиция плашки и монитор, к которому её привели."""
    target = pick_target_screen(infos, rect, cursor)
    if target is None:
        return QPoint(rect.x(), rect.y()), None
    return clamp_rect_into(rect, target.available, margin), target


def screen_binding(
    infos: list[ScreenInfo],
    rect: QRect,
    cursor: QPoint | None = None,
) -> "tuple[str, int, int] | None":
    """Привязка позиции плашки к монитору: (имя, dx, dy).

    Смещение отсчитывается от левого верхнего угла ПОЛНОЙ геометрии монитора,
    а не рабочей области: рабочая область съезжает вместе с панелью задач,
    начало координат монитора — нет.
    """
    target = pick_target_screen(infos, rect, cursor)
    if target is None or not target.name:
        return None
    origin = target.geometry.topLeft()
    return target.name, rect.x() - origin.x(), rect.y() - origin.y()


def position_from_binding(
    infos: list[ScreenInfo],
    name: str,
    dx: "int | None",
    dy: "int | None",
    hint: "QPoint | None" = None,
) -> "QPoint | None":
    """Абсолютная точка по сохранённой привязке. None — такого монитора нет.

    hint — прежняя абсолютная позиция плашки. Она нужна только для одного
    случая: Windows отдаёт в QScreen.name() модель монитора («PHL 278B1»),
    поэтому два ОДИНАКОВЫХ монитора называются одинаково, и по имени их не
    различить. Тогда из одноимённых выбирается тот, к которому прежняя
    позиция ближе. Монитор один — hint не используется вовсе.
    """
    if not name or dx is None or dy is None:
        return None
    try:
        offset_x, offset_y = int(dx), int(dy)
    except (TypeError, ValueError):
        return None
    matches = [info for info in infos if info.name == name]
    if not matches:
        return None
    if len(matches) > 1 and hint is not None:
        matches.sort(key=lambda i: _distance_to_rect(i.geometry, hint))
    origin = matches[0].geometry.topLeft()
    return QPoint(origin.x() + offset_x, origin.y() + offset_y)


class AudioLevelWaveform(QWidget):
    """Small rolling microphone-level waveform for the compact overlay."""

    BAR_COUNT = 7

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(20, 14)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAccessibleName("Уровень микрофона")
        self._levels = [0.0] * self.BAR_COUNT

    def set_level(self, level: float) -> None:
        target = float(level)
        if not math.isfinite(target):
            target = 0.0
        target = max(0.0, min(1.0, target))
        if target < 0.03:
            target = 0.0
        previous = self._levels[-1]
        smoothing = 0.72 if target > previous else 0.28
        smoothed = previous + (target - previous) * smoothing
        self._levels = [*self._levels[1:], smoothed]
        self.update()

    def reset(self) -> None:
        self._levels = [0.0] * self.BAR_COUNT
        self.update()

    def paintEvent(self, event) -> None:  # noqa: ANN001
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#ef4444"))

        bar_width = 2.0
        gap = (self.width() - self.BAR_COUNT * bar_width) / (self.BAR_COUNT - 1)
        min_height = 2.0
        max_height = float(self.height() - 2)
        for index, level in enumerate(self._levels):
            bar_height = min_height + level * (max_height - min_height)
            x = index * (bar_width + gap)
            y = (self.height() - bar_height) / 2.0
            painter.drawRoundedRect(QRectF(x, y, bar_width, bar_height), 1.0, 1.0)


class RecordingOverlay(QWidget):
    """Always-on-top status pill.

    Normal states are intentionally tiny: a colored dot plus one word. The
    adaptive transcript preview is used only when there was no detected target
    text field for final insertion.
    """

    copy_requested = Signal(str)
    # US-077: позиция сохраняется вместе с монитором —
    # (x, y, имя экрана, dx, dy от левого верхнего угла этого экрана).
    position_changed = Signal(int, int, str, int, int)
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
        # US-077: текущий режим компоновки (компактная плашка или превью)
        # и окно, за сменой монитора которого мы следим. Режим нужен,
        # чтобы пересчитать размеры при переезде на монитор с другим
        # масштабом изображения, не гадая о состоянии плашки.
        self._compact = True
        self._screen_signal_window = None
        self._relayout_in_progress = False
        self._hotkey = "Ctrl+Alt+Space"
        self._result_text = ""
        # _idle управляет только быстрым выбором модели по правому клику.
        # Политику старта/остановки записи определяет MainWindow.toggle_recording,
        # а не визуальное состояние overlay. _in_picker=True пока показан выбор
        # модели: на это время окно снова принимает фокус для popup QComboBox.
        self._idle = False
        self._recording = False
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
        self.level_waveform = AudioLevelWaveform()
        self.level_waveform.setVisible(False)
        self.dot_label = QLabel("●")
        self.dot_label.setObjectName("Dot")
        self.status_label = QLabel("Ready")
        self.top_row.addWidget(self.level_waveform)
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
        # US-077: перерисовывать плашку прямо в обработчике screenRemoved
        # нельзя — на этот момент Qt ещё не обновил список экранов.
        # Нулевой таймер откладывает её на следующий проход цикла событий.
        self._screen_refresh_timer = QTimer(self)
        self._screen_refresh_timer.setSingleShot(True)
        self._screen_refresh_timer.setInterval(0)
        self._screen_refresh_timer.timeout.connect(self._ensure_visible_on_screen)
        app = QApplication.instance()
        if app is not None:
            try:
                app.screenRemoved.connect(self._on_screen_removed)
            except (AttributeError, RuntimeError):  # noqa: BLE001
                pass
        self._place_default()
        self.show_idle()
        self._bind_screen_signals()

    def _place_default(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            self.move(40, 40)
            return
        geo = screen.availableGeometry()
        self._resize_to_content(compact=True)
        self.move(geo.right() - self.width() - 32, geo.top() + 32)

    # ── US-077: позиция плашки на нескольких мониторах ────────────────────

    def _screen_infos(self) -> list[ScreenInfo]:
        return screen_infos(QApplication.screens())

    def _current_rect(self) -> QRect:
        """Прямоугольник плашки на виртуальном рабочем столе."""
        size = self.size() if self.size().isValid() else self.sizeHint()
        width = max(size.width(), self.COMPACT_MIN_WIDTH)
        height = max(size.height(), self.COMPACT_HEIGHT)
        pos = self.pos()
        return QRect(pos.x(), pos.y(), width, height)

    def restore_position(
        self,
        x: int | None,
        y: int | None,
        screen_name: str = "",
        screen_dx: int | None = None,
        screen_dy: int | None = None,
    ) -> None:
        """Вернуть плашку туда, где её оставили (US-077).

        Сначала пробуем привязку к монитору: имя экрана + смещение внутри
        него. Монитора нет (отключили, переименовали) или конфиг старого
        формата без привязки — берём абсолютные координаты, а общий кламп сам
        положит плашку на ближайший оставшийся монитор целиком в видимую
        область.
        """
        infos = self._screen_infos()
        try:
            saved = None if x is None or y is None else QPoint(int(x), int(y))
        except (TypeError, ValueError):
            saved = None
        point = position_from_binding(infos, screen_name, screen_dx, screen_dy, saved)
        if point is None:
            if saved is None:
                return
            point = saved
        rect = self._current_rect()
        rect.moveTo(point)
        clamped, _ = clamp_overlay_rect(infos, rect)
        self.move(clamped)

    def current_screen_binding(self) -> "tuple[str, int, int] | None":
        """Текущая привязка позиции к монитору — (имя, dx, dy) или None."""
        return screen_binding(self._screen_infos(), self._current_rect())

    def _clamp_point_to_screens(self, point: QPoint, cursor: QPoint | None = None) -> QPoint:
        """Точка, при которой плашка видна целиком.

        cursor задаётся при перетаскивании: границы применяются к монитору
        ПОД КУРСОРОМ, а не к основному — иначе плашка у стыка мониторов
        перескакивала на основной экран.
        """
        rect = self._current_rect()
        rect.moveTo(point)
        clamped, _ = clamp_overlay_rect(self._screen_infos(), rect, cursor)
        return clamped

    def _ensure_visible_on_screen(self, cursor: QPoint | None = None) -> None:
        self.move(self._clamp_point_to_screens(self.pos(), cursor))

    def _bind_screen_signals(self) -> None:
        """Подписаться на переезд окна между мониторами (US-077).

        QWindow появляется только после создания окна, поэтому подписку
        обновляем при каждом показе, а не один раз в конструкторе.
        """
        handle = self.windowHandle()
        if handle is None or handle is self._screen_signal_window:
            return
        try:
            handle.screenChanged.connect(self._on_window_screen_changed)
        except (AttributeError, RuntimeError):  # noqa: BLE001
            return
        self._screen_signal_window = handle

    def showEvent(self, event) -> None:  # noqa: ANN001
        super().showEvent(event)
        self._bind_screen_signals()

    def _on_window_screen_changed(self, screen) -> None:  # noqa: ANN001
        """Плашка переехала на монитор с другим масштабом изображения.

        Размеры плашки заданы фиксированными пикселями, а метрики шрифта Qt
        считает под DPI того экрана, где виджет находился. Без пересчёта
        плашка на соседнем мониторе выглядит другого размера, а текст статуса
        обрезается. Масштабирование всего приложения при этом не трогаем —
        починка точечная, только для плашки (решение владельца продукта).
        """
        del screen
        self._relayout_for_screen()

    def _relayout_for_screen(self) -> None:
        # Пересчёт двигает и меняет размер окна, а это снова может увести
        # плашку на соседний монитор и вызвать screenChanged. Без защиты
        # от повторного входа на стыке экранов с разным масштабом плашка
        # могла бы задрожать между ними.
        if self._relayout_in_progress:
            return
        self._relayout_in_progress = True
        try:
            self._apply_screen_relayout()
        finally:
            self._relayout_in_progress = False

    def _apply_screen_relayout(self) -> None:
        before = self.size()
        self._resize_to_content(compact=self._compact)
        if self._drag_start is not None:
            # Во время перетаскивания плашка не должна ни прыгать по границам,
            # ни выскальзывать из-под курсора: точку захвата ужимаем под новый
            # размер, а кламп откладываем до отпускания кнопки.
            self._drag_start = QPoint(
                min(self._drag_start.x(), max(0, self.width() - 1)),
                min(self._drag_start.y(), max(0, self.height() - 1)),
            )
            return
        if self.size() != before:
            self._ensure_visible_on_screen()

    def _on_screen_removed(self, screen) -> None:  # noqa: ANN001
        """Монитор отключили — плашка обязана остаться в видимой области.

        Сохранённую привязку намеренно НЕ перезаписываем: вернут монитор —
        плашка снова окажется там, где её оставили.
        """
        del screen
        self._screen_refresh_timer.start()


    def _resize_to_content(self, *, compact: bool = False) -> None:
        # US-077: режим запоминается, чтобы пересчитать размеры при
        # переезде плашки на монитор с другим масштабом.
        self._compact = compact
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

    def _set_recording_visual(self, active: bool) -> None:
        changed = active != self._recording
        self._recording = active
        self.level_waveform.setVisible(active)
        self.dot_label.setVisible(not active)
        if changed:
            self.level_waveform.reset()

    def _set_state(
        self,
        status: str,
        dot_color: str,
        *,
        compact: bool,
        auto_ready_ms: int | None = None,
        recording: bool = False,
    ) -> None:
        self._return_timer.stop()
        # Любой обычный статус выводит плашку из режима пикера.
        if self._in_picker:
            self._exit_picker_mode()
        self._idle = False
        self._hide_picker_widgets()
        self.status_label.setText(status)
        self.dot_label.setText("●")
        self.dot_label.setStyleSheet(f"color: {dot_color};")
        self._set_recording_visual(recording)
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
        self._set_recording_visual(False)
        self.hide()

    def reset_for_new_recording(self, *, live_enabled: bool = False) -> None:
        self._return_timer.stop()
        self._result_text = ""
        self.preview_label.setText("")
        self.preview_label.setMinimumHeight(0)
        self.preview_label.setMaximumHeight(self.PREVIEW_MAX_HEIGHT)
        self.preview_label.setVisible(False)
        self.copy_btn.setVisible(False)
        self.level_waveform.reset()
        self.show_recording(0.0, live_enabled=live_enabled)

    def show_recording(self, elapsed: float = 0.0, *, live_enabled: bool = False) -> None:
        # Live is disabled in the stable build, so recording remains compact.
        self._set_state("Запись", "#ef4444", compact=True, recording=True)

    def set_audio_level(self, level: float) -> None:
        if self._recording:
            self.level_waveform.set_level(level)

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
        self._set_recording_visual(False)
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
        self._set_recording_visual(False)
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
            # US-077: во время перетаскивания плашка едет строго за курсором.
            # Границы применяются при отпускании — и к монитору под курсором.
            self.move(event.globalPosition().toPoint() - self._drag_start)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001
        dragged = self._drag_start is not None
        self._drag_start = None
        if event.button() == Qt.RightButton:
            event.accept()
            return
        cursor = event.globalPosition().toPoint() if dragged else None
        self._ensure_visible_on_screen(cursor)
        binding = screen_binding(self._screen_infos(), self._current_rect(), cursor)
        name, dx, dy = binding if binding is not None else ("", 0, 0)
        self.position_changed.emit(self.pos().x(), self.pos().y(), name, dx, dy)
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
