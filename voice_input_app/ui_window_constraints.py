from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtWidgets import QApplication, QFrame, QScrollArea, QVBoxLayout, QWidget

from . import ui as _ui

_EXTRA_APP_STYLE = """
QComboBox, QLineEdit, QKeySequenceEdit {
    min-height: 30px;
}
QScrollArea#SettingsScrollArea {
    background: #101114;
    border: none;
}
QWidget#SettingsScrollWrapper,
QWidget#SettingsScrollContent,
QWidget#SettingsScrollViewport {
    background: #101114;
}
"""

_PATCHED = False


def apply_patches() -> None:
    """Apply small UI-only fixes without touching recording/model logic."""
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    if _EXTRA_APP_STYLE not in _ui.APP_STYLE:
        _ui.APP_STYLE = _ui.APP_STYLE.rstrip() + "\n" + _EXTRA_APP_STYLE

    main_cls = _ui.MainWindow
    original_build_ui = main_cls._build_ui
    original_settings_tab = main_cls._settings_tab
    original_show_from_tray = main_cls.show_from_tray
    original_show_event = getattr(main_cls, "showEvent", None)
    original_change_event = getattr(main_cls, "changeEvent", None)
    original_resize_event = getattr(main_cls, "resizeEvent", None)

    def _available_screen_geometry(self):
        screen = None
        handle = self.windowHandle()
        if handle is not None:
            screen = handle.screen()
        if screen is None:
            screen = QApplication.screenAt(self.frameGeometry().center())
        if screen is None:
            screen = QApplication.primaryScreen()
        return screen.availableGeometry() if screen is not None else None

    def _fit_window_to_available_screen(self) -> None:
        if getattr(self, "_fitting_to_available_screen", False):
            return

        available = self._available_screen_geometry()
        if available is None or self.isFullScreen():
            return

        self._fitting_to_available_screen = True
        try:
            if not self.isMaximized():
                frame = self.frameGeometry()
                client = self.geometry()
                frame_extra_w = max(0, frame.width() - client.width())
                frame_extra_h = max(0, frame.height() - client.height())
                max_client_w = max(self.minimumWidth(), available.width() - frame_extra_w)
                max_client_h = max(self.minimumHeight(), available.height() - frame_extra_h)
                new_w = min(self.width(), max_client_w)
                new_h = min(self.height(), max_client_h)
                if new_w != self.width() or new_h != self.height():
                    self.resize(new_w, new_h)

            frame = self.frameGeometry()
            dx = 0
            dy = 0
            if frame.left() < available.left():
                dx = available.left() - frame.left()
            elif frame.right() > available.right():
                dx = available.right() - frame.right()

            if frame.top() < available.top():
                dy = available.top() - frame.top()
            elif frame.bottom() > available.bottom():
                dy = available.bottom() - frame.bottom()

            if dx or dy:
                self.move(self.x() + dx, self.y() + dy)
        finally:
            self._fitting_to_available_screen = False

    def _settings_tab(self):
        content = original_settings_tab(self)
        content.setObjectName("SettingsScrollContent")
        content.setAttribute(Qt.WA_StyledBackground, True)
        content.setMinimumHeight(content.sizeHint().height())

        wrapper = QWidget()
        wrapper.setObjectName("SettingsScrollWrapper")
        wrapper.setAttribute(Qt.WA_StyledBackground, True)
        outer = QVBoxLayout(wrapper)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea(wrapper)
        scroll.setObjectName("SettingsScrollArea")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.viewport().setObjectName("SettingsScrollViewport")
        scroll.viewport().setAttribute(Qt.WA_StyledBackground, True)
        scroll.setWidget(content)
        outer.addWidget(scroll)
        return wrapper

    def _build_ui(self):
        result = original_build_ui(self)
        QTimer.singleShot(0, self._fit_window_to_available_screen)
        return result

    def _show_from_tray(self) -> None:
        result = original_show_from_tray(self)
        QTimer.singleShot(0, self._fit_window_to_available_screen)
        return result

    def _show_event(self, event):
        if original_show_event is not None:
            original_show_event(self, event)
        QTimer.singleShot(0, self._fit_window_to_available_screen)

    def _change_event(self, event):
        if original_change_event is not None:
            original_change_event(self, event)
        if event.type() == QEvent.WindowStateChange:
            QTimer.singleShot(0, self._fit_window_to_available_screen)

    def _resize_event(self, event):
        if original_resize_event is not None:
            original_resize_event(self, event)
        if not getattr(self, "_fitting_to_available_screen", False):
            QTimer.singleShot(0, self._fit_window_to_available_screen)

    main_cls._available_screen_geometry = _available_screen_geometry
    main_cls._fit_window_to_available_screen = _fit_window_to_available_screen
    main_cls._settings_tab = _settings_tab
    main_cls._build_ui = _build_ui
    main_cls.show_from_tray = _show_from_tray
    main_cls.showEvent = _show_event
    main_cls.changeEvent = _change_event
    main_cls.resizeEvent = _resize_event
