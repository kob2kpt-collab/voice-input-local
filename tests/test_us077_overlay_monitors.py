# -*- coding: utf-8 -*-
r"""Регресс-тесты US-077: плашка на нескольких мониторах.

Дефект из отчёта владельца продукта: при перетаскивании плашки к стыку двух
мониторов она перескакивала на основной экран. Причина — `_clamp_point_to_screens`
искал монитор через `availableGeometry().contains(левый верхний угол)` и при
промахе откатывался на `primaryScreen()`. Промахнуться легко: полоса панели
задач, стык мониторов разной высоты, монитор правее или ниже основного.

Реальные мониторы в headless-окружении не эмулируются, поэтому проверяется
чистая логика над снимками `ScreenInfo` плюс поведение самой плашки с
подменённым списком экранов.

Запуск (в venv приложения):
    .venv\Scripts\python.exe tests\test_us077_overlay_monitors.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["LOCALAPPDATA"] = tempfile.gettempdir()

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, Qt, Signal  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from voice_input_app.config import AppConfig  # noqa: E402
from voice_input_app.overlay import (  # noqa: E402
    RecordingOverlay,
    ScreenInfo,
    clamp_overlay_rect,
    pick_target_screen,
    position_from_binding,
    screen_binding,
    screen_infos,
)

_APP = QApplication.instance() or QApplication(sys.argv)

UI_PATH = REPO_ROOT / "voice_input_app" / "ui.py"
OVERLAY_PATH = REPO_ROOT / "voice_input_app" / "overlay.py"

# Конфигурация из отчёта владельца: два монитора бок о бок, у каждого снизу
# полоса панели задач высотой 40 px. Именно эта полоса и стык между экранами
# были «мёртвой зоной», из-за которой плашка улетала на основной монитор.
LEFT = ScreenInfo(
    name=r"\\.\DISPLAY1",
    geometry=QRect(0, 0, 1920, 1080),
    available=QRect(0, 0, 1920, 1040),
)
RIGHT = ScreenInfo(
    name=r"\\.\DISPLAY2",
    geometry=QRect(1920, 0, 1920, 1080),
    available=QRect(1920, 0, 1920, 1040),
)
TWO_SCREENS = [LEFT, RIGHT]
PILL = (100, 26)  # ширина и высота компактной плашки


def _rect(x: int, y: int) -> QRect:
    return QRect(x, y, PILL[0], PILL[1])


def _method_source(path: Path, name: str) -> str:
    src = path.read_text(encoding="utf-8")
    start = src.index(f"    def {name}(")
    end = src.find("\n    def ", start + 10)
    return src[start: end if end != -1 else len(src)]


# ── (а) «мёртвая зона» не уводит плашку на основной монитор ───────────────

def test_dead_zone_keeps_overlay_on_its_own_screen() -> None:
    """Точка, не попавшая ни в одну availableGeometry, остаётся на своём экране."""
    # Плашка лежит на правом мониторе в полосе панели задач: ни одна
    # availableGeometry её левый верхний угол не содержит.
    rect = _rect(2400, 1050)
    assert not any(s.available.contains(rect.topLeft()) for s in TWO_SCREENS)

    assert pick_target_screen(TWO_SCREENS, rect) is RIGHT
    point, target = clamp_overlay_rect(TWO_SCREENS, rect)
    assert target is RIGHT
    # x не изменился — плашка не перескочила на основной монитор.
    assert point.x() == 2400
    # y подтянулся вверх ровно на высоту панели задач + отступ.
    assert RIGHT.available.contains(QRect(point.x(), point.y(), *PILL))

    # Прежняя логика (кламп по основному монитору) дала бы левый экран.
    assert not LEFT.available.contains(point)


def test_screen_below_and_right_is_not_replaced_by_primary() -> None:
    """Монитор ниже и правее основного тоже удерживает плашку."""
    lower = ScreenInfo(
        name="lower",
        geometry=QRect(1920, 1080, 1280, 1024),
        available=QRect(1920, 1080, 1280, 984),
    )
    screens = [LEFT, lower]
    rect = _rect(2500, 2000)
    assert pick_target_screen(screens, rect) is lower
    point, target = clamp_overlay_rect(screens, rect)
    assert target is lower
    assert lower.available.contains(QRect(point.x(), point.y(), *PILL))


def test_screen_with_largest_overlap_wins_at_the_seam() -> None:
    """На стыке выигрывает монитор, на котором плашки видно больше."""
    mostly_left = _rect(1830, 500)   # 90 px слева, 10 px справа
    mostly_right = _rect(1900, 500)  # 20 px слева, 80 px справа
    assert pick_target_screen(TWO_SCREENS, mostly_left) is LEFT
    assert pick_target_screen(TWO_SCREENS, mostly_right) is RIGHT


# ── (б) при перетаскивании монитор выбирается по курсору ──────────────────

def test_drag_uses_screen_under_cursor() -> None:
    """Пока идёт перетаскивание, границы берутся от монитора под курсором."""
    rect = _rect(1890, 500)  # плашка сидит на стыке
    assert pick_target_screen(TWO_SCREENS, rect, QPoint(1930, 500)) is RIGHT
    assert pick_target_screen(TWO_SCREENS, rect, QPoint(1900, 500)) is LEFT


def test_cursor_over_taskbar_still_resolves_to_its_screen() -> None:
    """Курсор над панелью задач принадлежит своему монитору, а не основному.

    Поиск по курсору идёт по ПОЛНОЙ геометрии монитора: по рабочей области
    курсор в полосе панели задач не нашёлся бы нигде, и мы вернулись бы
    к тому же дефекту.
    """
    cursor = QPoint(2400, 1060)
    assert not any(s.available.contains(cursor) for s in TWO_SCREENS)
    assert pick_target_screen(TWO_SCREENS, _rect(2400, 1040), cursor) is RIGHT


def test_cursor_outside_all_screens_picks_nearest_not_primary() -> None:
    """Курсор вне всех мониторов → ближайший монитор, а не основной."""
    assert pick_target_screen(TWO_SCREENS, _rect(3800, 500), QPoint(4000, 500)) is RIGHT


def test_overlay_release_clamps_to_screen_under_cursor() -> None:
    """Отпускание кнопки над правым монитором не возвращает плашку на левый."""
    overlay = RecordingOverlay()
    try:
        overlay._screen_infos = lambda: TWO_SCREENS  # noqa: SLF001
        overlay.show_idle()
        emitted: list[tuple] = []
        overlay.position_changed.connect(lambda *args: emitted.append(args))

        overlay._drag_start = QPoint(10, 10)  # noqa: SLF001
        overlay.move(2400, 1050)  # правый монитор, полоса панели задач
        release = QMouseEvent(
            QEvent.Type.MouseButtonRelease,
            QPointF(10, 10),
            QPointF(2410, 1060),
            Qt.LeftButton,
            Qt.NoButton,
            Qt.NoModifier,
        )
        overlay.mouseReleaseEvent(release)
        _APP.processEvents()

        assert overlay.pos().x() == 2400, overlay.pos()
        assert emitted, "position_changed не сработал"
        x, y, name, dx, dy = emitted[-1]
        assert name == RIGHT.name
        assert dx == x - RIGHT.geometry.x()
        assert dy == y - RIGHT.geometry.y()
        assert RIGHT.available.contains(QRect(x, y, overlay.width(), overlay.height()))
    finally:
        overlay.close()


# ── (в) позиция сохраняется и восстанавливается вместе с монитором ────────

def test_binding_is_relative_to_its_monitor() -> None:
    rect = _rect(2400, 300)
    binding = screen_binding(TWO_SCREENS, rect)
    assert binding == (RIGHT.name, 480, 300)
    assert position_from_binding(TWO_SCREENS, *binding) == QPoint(2400, 300)


def test_binding_follows_monitor_when_layout_changes() -> None:
    """Монитор переставили влево — плашка едет вместе с ним, а не остаётся координатой."""
    binding = screen_binding(TWO_SCREENS, _rect(2400, 300))
    moved_right = ScreenInfo(
        name=RIGHT.name,
        geometry=QRect(-1920, 0, 1920, 1080),
        available=QRect(-1920, 0, 1920, 1040),
    )
    assert position_from_binding([LEFT, moved_right], *binding) == QPoint(-1440, 300)


def test_identical_monitors_share_a_name_and_are_told_apart_by_position() -> None:
    """Два одинаковых монитора Windows называет одинаково (модель из EDID).

    Различить их по имени нельзя, поэтому из одноимённых берётся тот, к
    которому ближе прежняя абсолютная позиция плашки.
    """
    twin_left = ScreenInfo(name="PHL 278B1", geometry=QRect(0, 0, 1920, 1080),
                           available=QRect(0, 0, 1920, 1040))
    twin_right = ScreenInfo(name="PHL 278B1", geometry=QRect(1920, 0, 1920, 1080),
                            available=QRect(1920, 0, 1920, 1040))
    twins = [twin_left, twin_right]
    # Плашку оставляли на правом близнеце: dx=480 от его начала координат.
    assert position_from_binding(twins, "PHL 278B1", 480, 300, QPoint(2400, 300)) == QPoint(2400, 300)
    # И на левом — та же привязка, другая подсказка.
    assert position_from_binding(twins, "PHL 278B1", 480, 300, QPoint(480, 300)) == QPoint(480, 300)
    # Без подсказки берётся первый одноимённый — поведение определено.
    assert position_from_binding(twins, "PHL 278B1", 480, 300) == QPoint(480, 300)


def test_overlay_restores_position_with_monitor() -> None:
    overlay = RecordingOverlay()
    try:
        overlay._screen_infos = lambda: TWO_SCREENS  # noqa: SLF001
        overlay.show_idle()
        overlay.restore_position(0, 0, RIGHT.name, 480, 300)
        assert overlay.pos() == QPoint(2400, 300)
        binding = overlay.current_screen_binding()
        assert binding is not None and binding[0] == RIGHT.name
    finally:
        overlay.close()


def test_config_keeps_monitor_binding() -> None:
    cfg = AppConfig()
    assert cfg.overlay_screen_name == ""
    assert cfg.overlay_screen_dx is None and cfg.overlay_screen_dy is None
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        cfg.overlay_x, cfg.overlay_y = 2400, 300
        cfg.overlay_screen_name = RIGHT.name
        cfg.overlay_screen_dx, cfg.overlay_screen_dy = 480, 300
        cfg.save(path)
        again = AppConfig.load(path)
        assert again.overlay_screen_name == RIGHT.name
        assert (again.overlay_screen_dx, again.overlay_screen_dy) == (480, 300)
        assert (again.overlay_x, again.overlay_y) == (2400, 300)


# ── (г) старый конфиг без привязки к монитору ─────────────────────────────

def test_old_config_without_binding_still_loads() -> None:
    """config.json прежнего формата открывается и работает как раньше."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(
            json.dumps({"overlay_enabled": True, "overlay_x": 1500, "overlay_y": 200}),
            encoding="utf-8",
        )
        cfg = AppConfig.load(path)
        assert (cfg.overlay_x, cfg.overlay_y) == (1500, 200)
        assert cfg.overlay_screen_name == ""
        assert cfg.overlay_screen_dx is None and cfg.overlay_screen_dy is None


def test_restore_without_binding_falls_back_to_absolute() -> None:
    overlay = RecordingOverlay()
    try:
        overlay._screen_infos = lambda: TWO_SCREENS  # noqa: SLF001
        overlay.show_idle()
        overlay.restore_position(1500, 200, "", None, None)
        assert overlay.pos() == QPoint(1500, 200)
    finally:
        overlay.close()


def test_main_window_migrates_old_config_binding() -> None:
    """Старый конфиг дополняется привязкой при восстановлении позиции."""
    src = UI_PATH.read_text(encoding="utf-8")
    assert "self.cfg.overlay_screen_name," in src
    assert "self.cfg.overlay_screen_dx," in src
    assert "self.cfg.overlay_screen_dy," in src
    assert "if not self.cfg.overlay_screen_name:" in src
    assert "self.overlay.current_screen_binding()" in src
    handler = _method_source(UI_PATH, "on_overlay_position_changed")
    for token in ("screen_name", "screen_dx", "screen_dy", "self.cfg.save()"):
        assert token in handler, token


# ── (д) отключение монитора возвращает плашку в видимую область ───────────

def test_lost_monitor_returns_overlay_to_remaining_screen() -> None:
    """Монитора из привязки больше нет → плашка целиком на оставшемся."""
    rect = _rect(2400, 300)  # координаты исчезнувшего правого монитора
    assert position_from_binding([LEFT], RIGHT.name, 480, 300) is None
    point, target = clamp_overlay_rect([LEFT], rect)
    assert target is LEFT
    assert LEFT.available.contains(QRect(point.x(), point.y(), *PILL))


def test_overlay_restore_survives_disconnected_monitor() -> None:
    overlay = RecordingOverlay()
    try:
        overlay._screen_infos = lambda: [LEFT]  # noqa: SLF001
        overlay.show_idle()
        overlay.restore_position(2400, 300, RIGHT.name, 480, 300)
        pos = overlay.pos()
        assert LEFT.available.contains(QRect(pos.x(), pos.y(), overlay.width(), overlay.height()))
    finally:
        overlay.close()


def test_screen_removed_is_handled_without_touching_saved_binding() -> None:
    overlay = RecordingOverlay()
    try:
        overlay._screen_infos = lambda: [LEFT]  # noqa: SLF001
        overlay.show_idle()
        emitted: list[tuple] = []
        overlay.position_changed.connect(lambda *args: emitted.append(args))
        overlay.move(2400, 300)
        overlay._on_screen_removed(None)  # noqa: SLF001
        assert overlay._screen_refresh_timer.isActive()  # noqa: SLF001
        _APP.processEvents()
        pos = overlay.pos()
        assert LEFT.available.contains(QRect(pos.x(), pos.y(), overlay.width(), overlay.height()))
        # Сохранённую позицию отключение монитора не перезаписывает.
        assert not emitted
    finally:
        overlay.close()


# ── (в дополнение) масштаб монитора: пересчёт размеров плашки ─────────────

def test_screen_change_recomputes_size_for_current_layout() -> None:
    """Переезд на монитор с другим масштабом пересчитывает размеры плашки."""
    overlay = RecordingOverlay()
    try:
        overlay._screen_infos = lambda: TWO_SCREENS  # noqa: SLF001
        overlay.show_idle()
        assert overlay._compact is True  # noqa: SLF001
        overlay._on_window_screen_changed(None)  # noqa: SLF001
        _APP.processEvents()
        assert overlay.height() == overlay.COMPACT_HEIGHT
        assert overlay.width() <= overlay.COMPACT_MAX_WIDTH

        overlay.show_result_text("Длинная расшифровка, которую нельзя обрезать " * 3)
        assert overlay._compact is False  # noqa: SLF001
        preview_size = overlay.size()
        overlay._on_window_screen_changed(None)  # noqa: SLF001
        _APP.processEvents()
        # Развёрнутое состояние осталось развёрнутым, текст не потерян.
        assert overlay._compact is False  # noqa: SLF001
        assert overlay.size() == preview_size
        assert overlay.preview_label.isVisible()
        assert overlay.height() >= overlay.preview_label.minimumHeight()
    finally:
        overlay.close()


def test_screen_change_during_drag_does_not_clamp() -> None:
    """Во время перетаскивания пересчёт размеров не тащит плашку по границам."""
    overlay = RecordingOverlay()
    try:
        overlay._screen_infos = lambda: TWO_SCREENS  # noqa: SLF001
        overlay.show_idle()
        overlay._drag_start = QPoint(10, 10)  # noqa: SLF001
        overlay.move(2400, 1060)  # за нижней границей рабочей области
        overlay._on_window_screen_changed(None)  # noqa: SLF001
        _APP.processEvents()
        assert overlay.pos() == QPoint(2400, 1060)
        # Точка захвата осталась внутри плашки.
        assert 0 <= overlay._drag_start.x() < max(1, overlay.width())  # noqa: SLF001
    finally:
        overlay.close()


def test_overlay_subscribes_to_screen_change() -> None:
    overlay = RecordingOverlay()
    try:
        overlay.show()
        _APP.processEvents()
        assert overlay._screen_signal_window is not None  # noqa: SLF001
        assert overlay._screen_signal_window is overlay.windowHandle()  # noqa: SLF001
    finally:
        overlay.close()


# ── TASK-386: масштаб меняется на ТОМ ЖЕ мониторе ─────────────────────────


class _FakeScreen(QObject):
    """Экран с теми же сигналами метрик, что у QScreen.

    Нужен там, где важна САМА подписка (переезд между экранами, отписка):
    подменить список мониторов у Qt в headless-окружении нельзя.
    """

    logicalDotsPerInchChanged = Signal(float)
    physicalDotsPerInchChanged = Signal(float)
    geometryChanged = Signal(QRect)
    availableGeometryChanged = Signal(QRect)

    def __init__(self, label: str) -> None:
        super().__init__()
        self.label = label


class _DeletedScreen:
    """Заглушка снесённого Qt-объекта: любое обращение к нему — RuntimeError.

    Записывает попытки обращения, чтобы тест мог доказать, что код к
    удалённому экрану не потянулся вовсе.
    """

    def __init__(self) -> None:
        object.__setattr__(self, "touched", [])

    def __getattr__(self, name):  # noqa: ANN001, ANN204
        object.__getattribute__(self, "touched").append(name)
        raise RuntimeError("Internal C++ object (QScreen) already deleted.")


def _relayout_probe(overlay: RecordingOverlay) -> list:
    """Записывать вызовы пересчёта, не отменяя его."""
    calls: list = []
    original = overlay._relayout_for_screen  # noqa: SLF001

    def spy(**kwargs) -> None:  # noqa: ANN003
        calls.append(kwargs)
        original(**kwargs)

    overlay._relayout_for_screen = spy  # noqa: SLF001
    return calls


def test_scale_change_on_same_monitor_triggers_relayout() -> None:
    """Пользователь сменил масштаб в параметрах Windows, плашку не двигал."""
    overlay = RecordingOverlay()
    try:
        overlay.show()
        _APP.processEvents()
        screen = overlay._metrics_screen  # noqa: SLF001
        assert screen is not None, "подписка на метрики экрана не встала"
        assert overlay._metrics_signal_names, "не подключён ни один сигнал"  # noqa: SLF001

        calls = _relayout_probe(overlay)
        screen.logicalDotsPerInchChanged.emit(120.0)
        QTest.qWait(120)
        _APP.processEvents()
        assert calls == [{"ensure_visible": True}], calls
        # Пересчёт возвращает плашку в видимую область: рабочая область
        # монитора при смене масштаба меняет размер.
        assert overlay.height() == overlay.COMPACT_HEIGHT
    finally:
        overlay.close()


def test_logical_dpi_alone_is_not_the_only_subscription() -> None:
    """В Qt6 логический DPI нормализован к 96 и на масштаб Windows не реагирует.

    Поэтому подписка обязана включать геометрию экрана — иначе смена
    масштаба прошла бы мимо нас.
    """
    assert "geometryChanged" in RecordingOverlay.SCREEN_METRIC_SIGNALS
    assert "logicalDotsPerInchChanged" in RecordingOverlay.SCREEN_METRIC_SIGNALS
    overlay = RecordingOverlay()
    try:
        overlay.show()
        _APP.processEvents()
        screen = overlay._metrics_screen  # noqa: SLF001
        calls = _relayout_probe(overlay)
        screen.geometryChanged.emit(screen.geometry())
        QTest.qWait(120)
        _APP.processEvents()
        assert calls == [{"ensure_visible": True}], calls
    finally:
        overlay.close()


def test_signal_burst_gives_exactly_one_relayout() -> None:
    """Одна смена масштаба даёт всплеск сигналов — пересчёт должен быть один."""
    overlay = RecordingOverlay()
    try:
        overlay.show()
        _APP.processEvents()
        screen = overlay._metrics_screen  # noqa: SLF001
        calls = _relayout_probe(overlay)
        screen.logicalDotsPerInchChanged.emit(120.0)
        screen.physicalDotsPerInchChanged.emit(120.0)
        screen.geometryChanged.emit(screen.geometry())
        screen.availableGeometryChanged.emit(screen.availableGeometry())
        assert calls == [], "пересчёт не должен идти прямо из сигнала"
        QTest.qWait(120)
        _APP.processEvents()
        assert calls == [{"ensure_visible": True}], calls
    finally:
        overlay.close()


def test_metrics_path_respects_reentrancy_guard() -> None:
    """Защита от повторного входа покрывает и путь смены масштаба."""
    overlay = RecordingOverlay()
    try:
        applied: list = []
        overlay._apply_screen_relayout = lambda **kw: applied.append(kw)  # noqa: SLF001
        overlay._relayout_in_progress = True  # noqa: SLF001
        overlay._relayout_after_metrics_change()  # noqa: SLF001
        assert applied == []
        overlay._relayout_in_progress = False  # noqa: SLF001
        overlay._relayout_after_metrics_change()  # noqa: SLF001
        assert applied == [{"ensure_visible": True}]
    finally:
        overlay.close()


def test_metrics_subscription_moves_with_the_overlay() -> None:
    """Переезд на другой монитор переносит и подписку на масштаб.

    Иначе после перехода мы слушали бы масштаб прежнего экрана.
    """
    overlay = RecordingOverlay()
    first = _FakeScreen("first")
    second = _FakeScreen("second")
    try:
        overlay.show()
        _APP.processEvents()
        overlay._current_screen = lambda: first  # noqa: SLF001
        overlay._bind_screen_metrics_signals()  # noqa: SLF001
        assert overlay._metrics_screen is first  # noqa: SLF001

        # Плашка переехала: сигнал окна о смене экрана переносит подписку.
        overlay._current_screen = lambda: second  # noqa: SLF001
        overlay._on_window_screen_changed(None)  # noqa: SLF001
        assert overlay._metrics_screen is second  # noqa: SLF001

        calls = _relayout_probe(overlay)
        first.logicalDotsPerInchChanged.emit(120.0)
        QTest.qWait(120)
        _APP.processEvents()
        assert calls == [], "прежний экран всё ещё дёргает пересчёт"

        second.logicalDotsPerInchChanged.emit(120.0)
        QTest.qWait(120)
        _APP.processEvents()
        assert calls == [{"ensure_visible": True}], calls
    finally:
        overlay.close()


def test_repeated_binding_does_not_duplicate_subscription() -> None:
    """Повторный показ плашки не добавляет второе соединение к тому же экрану."""
    overlay = RecordingOverlay()
    screen = _FakeScreen("only")
    try:
        overlay._current_screen = lambda: screen  # noqa: SLF001
        for _ in range(3):
            overlay._bind_screen_metrics_signals()  # noqa: SLF001
        assert overlay._metrics_screen is screen  # noqa: SLF001
        calls = _relayout_probe(overlay)
        screen.logicalDotsPerInchChanged.emit(120.0)
        QTest.qWait(120)
        _APP.processEvents()
        assert calls == [{"ensure_visible": True}], calls
    finally:
        overlay.close()


def test_removed_screen_leaves_no_subscription() -> None:
    """Отключение монитора снимает подписку, не трогая удалённый объект Qt."""
    overlay = RecordingOverlay()
    try:
        deleted = _DeletedScreen()
        overlay._metrics_screen = deleted  # noqa: SLF001
        overlay._metrics_signal_names = list(RecordingOverlay.SCREEN_METRIC_SIGNALS)  # noqa: SLF001
        overlay._current_screen = lambda: None  # noqa: SLF001

        overlay._on_screen_removed(deleted)  # noqa: SLF001

        assert deleted.touched == [], deleted.touched
        assert overlay._metrics_screen is None  # noqa: SLF001
        assert overlay._metrics_signal_names == []  # noqa: SLF001
        assert overlay._screen_refresh_timer.isActive()  # noqa: SLF001
        _APP.processEvents()
        assert overlay._metrics_screen is None  # noqa: SLF001
    finally:
        overlay.close()


def test_removing_another_screen_keeps_our_subscription() -> None:
    """Отключили чужой монитор — наша подписка остаётся рабочей."""
    overlay = RecordingOverlay()
    ours = _FakeScreen("ours")
    other = _FakeScreen("other")
    try:
        overlay._current_screen = lambda: ours  # noqa: SLF001
        overlay._bind_screen_metrics_signals()  # noqa: SLF001
        overlay._on_screen_removed(other)  # noqa: SLF001
        _APP.processEvents()
        assert overlay._metrics_screen is ours  # noqa: SLF001
        calls = _relayout_probe(overlay)
        ours.logicalDotsPerInchChanged.emit(120.0)
        QTest.qWait(120)
        _APP.processEvents()
        assert calls == [{"ensure_visible": True}], calls
    finally:
        overlay.close()


def test_disconnect_survives_a_screen_deleted_behind_our_back() -> None:
    """Экран снесли без сигнала screenRemoved — отписка не должна падать."""
    overlay = RecordingOverlay()
    try:
        overlay._metrics_screen = _DeletedScreen()  # noqa: SLF001
        overlay._metrics_signal_names = list(RecordingOverlay.SCREEN_METRIC_SIGNALS)  # noqa: SLF001
        overlay._disconnect_screen_metrics()  # noqa: SLF001
        assert overlay._metrics_screen is None  # noqa: SLF001
        # И подписка на новый экран после этого встаёт как обычно.
        fresh = _FakeScreen("fresh")
        overlay._current_screen = lambda: fresh  # noqa: SLF001
        overlay._bind_screen_metrics_signals()  # noqa: SLF001
        assert overlay._metrics_screen is fresh  # noqa: SLF001
    finally:
        overlay.close()


def test_binding_a_deleted_screen_does_not_crash() -> None:
    """Подписка на уже снесённый экран не роняет плашку."""
    overlay = RecordingOverlay()
    try:
        overlay._current_screen = lambda: _DeletedScreen()  # noqa: SLF001
        overlay._bind_screen_metrics_signals()  # noqa: SLF001
        assert overlay._metrics_screen is None  # noqa: SLF001
        assert overlay._metrics_signal_names == []  # noqa: SLF001
    finally:
        overlay.close()


# ── статические гарантии ──────────────────────────────────────────────────

def test_clamp_no_longer_falls_back_to_primary_screen() -> None:
    """AC2: ограничение применяется к своему монитору, не к основному."""
    clamp = _method_source(OVERLAY_PATH, "_clamp_point_to_screens")
    assert "primaryScreen" not in clamp, clamp
    ensure = _method_source(OVERLAY_PATH, "_ensure_visible_on_screen")
    assert "primaryScreen" not in ensure, ensure


def test_overlay_window_flags_untouched() -> None:
    """Красная линия: плашка по-прежнему не крадёт фокус."""
    src = OVERLAY_PATH.read_text(encoding="utf-8")
    assert "Qt.Tool" in src
    assert "WA_ShowWithoutActivating" in src
    assert "WindowDoesNotAcceptFocus" in src
    assert "WA_TransparentForMouseEvents" in src
    exit_picker = _method_source(OVERLAY_PATH, "_exit_picker_mode")
    assert "self._set_accepts_focus(False)" in exit_picker
    assert "self._set_show_without_activating(True)" in exit_picker


def test_screen_infos_reads_real_qscreens() -> None:
    infos = screen_infos(QApplication.screens())
    assert infos, "Qt не отдал ни одного экрана"
    for info in infos:
        assert isinstance(info, ScreenInfo)
        assert info.geometry.isValid() and info.available.isValid()


def _run() -> None:
    tests = [
        test_dead_zone_keeps_overlay_on_its_own_screen,
        test_screen_below_and_right_is_not_replaced_by_primary,
        test_screen_with_largest_overlap_wins_at_the_seam,
        test_drag_uses_screen_under_cursor,
        test_cursor_over_taskbar_still_resolves_to_its_screen,
        test_cursor_outside_all_screens_picks_nearest_not_primary,
        test_overlay_release_clamps_to_screen_under_cursor,
        test_binding_is_relative_to_its_monitor,
        test_binding_follows_monitor_when_layout_changes,
        test_identical_monitors_share_a_name_and_are_told_apart_by_position,
        test_overlay_restores_position_with_monitor,
        test_config_keeps_monitor_binding,
        test_old_config_without_binding_still_loads,
        test_restore_without_binding_falls_back_to_absolute,
        test_main_window_migrates_old_config_binding,
        test_lost_monitor_returns_overlay_to_remaining_screen,
        test_overlay_restore_survives_disconnected_monitor,
        test_screen_removed_is_handled_without_touching_saved_binding,
        test_screen_change_recomputes_size_for_current_layout,
        test_screen_change_during_drag_does_not_clamp,
        test_overlay_subscribes_to_screen_change,
        test_scale_change_on_same_monitor_triggers_relayout,
        test_logical_dpi_alone_is_not_the_only_subscription,
        test_signal_burst_gives_exactly_one_relayout,
        test_metrics_path_respects_reentrancy_guard,
        test_metrics_subscription_moves_with_the_overlay,
        test_repeated_binding_does_not_duplicate_subscription,
        test_removed_screen_leaves_no_subscription,
        test_removing_another_screen_keeps_our_subscription,
        test_disconnect_survives_a_screen_deleted_behind_our_back,
        test_binding_a_deleted_screen_does_not_crash,
        test_clamp_no_longer_falls_back_to_primary_screen,
        test_overlay_window_flags_untouched,
        test_screen_infos_reads_real_qscreens,
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print("US-077 overlay monitors regression: ALL PASS")


if __name__ == "__main__":
    _run()
