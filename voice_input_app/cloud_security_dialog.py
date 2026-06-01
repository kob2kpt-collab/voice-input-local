"""US-018: уведомления безопасности при переключении на облачные модели.

Модуль вынесен из ui.py намеренно (см. CLAUDE.md → «Безопасное редактирование
файлов»): ui.py >100 КБ, поэтому крупные UI-блоки лучше держать в отдельном
небольшом файле, а в ui.py делать минимальные точечные правки.

Два сценария (решение владельца продукта):

* Внешняя облачная модель (по умолчанию) — янтарно-красный диалог-предупреждение
  с заметной ЗЕЛЁНОЙ врезкой про безопасный путь Cloud.ru и ссылкой на консоль.
  Подавляется в рамках сессии (логика подавления — в ui.py, не здесь).
* Безопасная внутренняя модель Cloud.ru (пользователь заранее пометил эндпоинт)
  — крупное зелёное подтверждение, показывается однократно за сессию.

Приложение НЕ классифицирует безопасность само — это ответственность
пользователя. Пометить эндпоинт «безопасным» можно ТОЛЬКО если его base_url
содержит домен cloud.ru (см. host_is_cloudru). Только внутри этого домена мы
считаем, что существуют безопасные эндпоинты.
"""

from __future__ import annotations

from urllib.parse import urlparse

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QLabel,
    QVBoxLayout,
    QWidget,
)

# Ссылка на список foundation-моделей Cloud.ru (кнопка «Внутренние» фильтрует
# модели, развёрнутые в защищённом контуре без выхода в интернет).
CLOUDRU_CONSOLE_URL = (
    "https://console.cloud.ru/spa/ml-foundation-models/models"
    "?projectId=b1fd69ce-ae7a-4f24-91fc-194a0d4998fc"
    "&customerId=77e53117-ef87-4b63-9118-0f839173186a"
)


def normalize_endpoint(url: str) -> str:
    """Привести base_url к каноничному виду для сравнения/хранения.

    Нижний регистр, без хвостовых слэшей и пробелов. Пустая строка → "".
    """
    if not url:
        return ""
    return url.strip().rstrip("/").lower()


def host_is_cloudru(url: str) -> bool:
    """True, если хост URL принадлежит домену cloud.ru.

    Только внутри cloud.ru пользователь может пометить эндпоинт «безопасным».
    Принимает URL и со схемой, и без неё ("foo.cloud.ru/v1").
    """
    if not url:
        return False
    raw = url.strip().lower()
    if "://" not in raw:
        raw = "https://" + raw
    try:
        host = urlparse(raw).hostname or ""
    except Exception:  # noqa: BLE001
        return False
    return host == "cloud.ru" or host.endswith(".cloud.ru")


# Цвета врезок (тёмная тема приложения).
_GREEN_BORDER = "#2e7d32"
_GREEN_BG = "#13301c"
_GREEN_TEXT = "#a5d6a7"
_AMBER_TEXT = "#ffb74d"


def _green_box(text: str) -> QFrame:
    box = QFrame()
    box.setStyleSheet(
        f"QFrame {{ background: {_GREEN_BG}; border: 1px solid {_GREEN_BORDER};"
        f" border-radius: 6px; }}"
    )
    lay = QVBoxLayout(box)
    lay.setContentsMargins(12, 10, 12, 10)
    title = QLabel("✅ Безопасный сценарий — внутренние модели Cloud.ru")
    title.setStyleSheet(
        f"color: {_GREEN_TEXT}; font-weight: bold; font-size: 14px; border: none;"
    )
    title.setWordWrap(True)
    body = QLabel(text)
    body.setWordWrap(True)
    body.setStyleSheet(f"color: {_GREEN_TEXT}; border: none;")
    link = QLabel(
        f'<a href="{CLOUDRU_CONSOLE_URL}" style="color:#80cbc4;">'
        "Открыть список моделей Cloud.ru → нажмите «Внутренние»</a>"
    )
    link.setOpenExternalLinks(True)
    link.setWordWrap(True)
    link.setStyleSheet("border: none;")
    lay.addWidget(title)
    lay.addWidget(body)
    lay.addWidget(link)
    return box


def confirm_external_switch(
    parent: QWidget | None,
    *,
    provider_label: str,
    endpoint: str,
    can_mark_safe: bool,
) -> tuple[bool, bool]:
    """Диалог-предупреждение для ВНЕШНЕЙ облачной модели.

    Возвращает (accepted, mark_safe):
      * accepted  — пользователь нажал «Продолжить» (False = «Отмена»).
      * mark_safe — пользователь отметил «Это внутренний эндпоинт Cloud.ru»
                    (доступно только при can_mark_safe=True).
    """
    dlg = QDialog(parent)
    dlg.setWindowTitle("Переключение на облачную модель")
    dlg.setModal(True)
    dlg.setMinimumWidth(520)
    layout = QVBoxLayout(dlg)
    layout.setSpacing(12)

    warn = QLabel(
        f"<b style='color:{_AMBER_TEXT};'>⚠ Внимание.</b> "
        f"Модель «{provider_label}» передаёт аудио (а для расшифровки — и файлы) "
        f"на внешние серверы:<br><code>{endpoint or '—'}</code><br><br>"
        "Убедитесь, что в передаваемых данных нет конфиденциальной или "
        "персональной информации."
    )
    warn.setTextFormat(Qt.RichText)
    warn.setWordWrap(True)
    layout.addWidget(warn)

    layout.addWidget(_green_box(
        "Модели, развёрнутые сервисом Cloud.ru на своих серверах в защищённом "
        "контуре, не имеют доступа в интернет и соответствуют требованиям "
        "регулятора по работе с персональными данными. С ними можно работать "
        "так же, как с локальными. Это модели из раздела «Внутренние»."
    ))

    mark_check = QCheckBox("Это внутренний эндпоинт Cloud.ru — пометить как безопасный")
    mark_check.setEnabled(can_mark_safe)
    if not can_mark_safe:
        mark_check.setToolTip("Доступно только для эндпоинтов с доменом cloud.ru.")
        hint = QLabel("Пометка доступна только если Base URL содержит домен cloud.ru.")
        hint.setObjectName("Subtitle")
        hint.setWordWrap(True)
    else:
        hint = None
    layout.addWidget(mark_check)
    if hint is not None:
        layout.addWidget(hint)

    buttons = QDialogButtonBox()
    ok_btn = buttons.addButton("Продолжить", QDialogButtonBox.AcceptRole)
    buttons.addButton("Отмена", QDialogButtonBox.RejectRole)
    ok_btn.setDefault(True)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    layout.addWidget(buttons)

    accepted = dlg.exec() == QDialog.Accepted
    mark_safe = bool(accepted and can_mark_safe and mark_check.isChecked())
    return accepted, mark_safe


def confirm_safe_switch(
    parent: QWidget | None,
    *,
    provider_label: str,
    endpoint: str,
) -> bool:
    """Зелёное подтверждение для БЕЗОПАСНОЙ внутренней модели Cloud.ru.

    Информационное окно (всегда возвращает True), показывается однократно за
    сессию — управление частотой в ui.py.
    """
    dlg = QDialog(parent)
    dlg.setWindowTitle("Безопасная облачная модель Cloud.ru")
    dlg.setModal(True)
    dlg.setMinimumWidth(520)
    layout = QVBoxLayout(dlg)
    layout.setSpacing(12)

    banner = QFrame()
    banner.setStyleSheet(
        f"QFrame {{ background: {_GREEN_BG}; border: 2px solid {_GREEN_BORDER};"
        f" border-radius: 8px; }}"
    )
    blay = QVBoxLayout(banner)
    blay.setContentsMargins(16, 14, 16, 14)
    title = QLabel("✅ Безопасно: внутренняя модель Cloud.ru")
    title.setStyleSheet(
        f"color: {_GREEN_TEXT}; font-weight: bold; font-size: 16px; border: none;"
    )
    title.setWordWrap(True)
    body = QLabel(
        f"Эндпоинт <code>{endpoint or '—'}</code> помечен как внутренний "
        "контур Cloud.ru. Данные не покидают защищённый контур, нет выхода в "
        "интернет, соответствует требованиям регулятора по работе с "
        "персональными данными.<br><br>"
        "<b>Можно работать так же, как с локальной моделью.</b>"
    )
    body.setTextFormat(Qt.RichText)
    body.setWordWrap(True)
    body.setStyleSheet(f"color: {_GREEN_TEXT}; border: none;")
    blay.addWidget(title)
    blay.addWidget(body)
    layout.addWidget(banner)

    buttons = QDialogButtonBox()
    ok_btn = buttons.addButton("Понятно", QDialogButtonBox.AcceptRole)
    ok_btn.setDefault(True)
    buttons.accepted.connect(dlg.accept)
    layout.addWidget(buttons)

    dlg.exec()
    return True
