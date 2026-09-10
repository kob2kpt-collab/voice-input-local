# -*- coding: utf-8 -*-
"""Регресс-тест US-084: обновление предлагается кнопкой в шапке.

Смысл истории: человек запускает программу, чтобы диктовать, и экран об
обновлении при запуске ему мешает. Поэтому проверки закрепляют главное — что
проверка обновлений САМА никакого экрана не открывает, а кнопка в шапке
переключается между «Проверить обновление» и «Обновить».

Проверяет (статически, разбором AST — как tests/test_us068_no_keyboard_capture.py):

1. Автоматическая проверка не открывает экран обновления (AC 1, AC 5):
   в on_update_check_result показ экрана возможен только при manual.
2. Кнопка живёт в шапке, а не в настройках (AC 2): header_update_btn создаётся
   в сборке шапки, check_updates_btn из настроек убрана.
3. Кнопка переключается в «Обновить» и обратно (AC 3, AC 7) —
   _refresh_header_update_button вызывается и после проверки, и после отказа.
4. Есть повторная тихая проверка во время работы (AC 4), её таймер — атрибут
   окна (иначе really_quit его не погасит, US-049).
5. Блок «Что нового» на экране обновления свёрнут и разворачивается (AC 6).
6. Машинная политика прячет кнопку в шапке целиком (AC 8).
7. Централизованное обновление US-057/US-058 не затронуто (AC 9).

Запуск: python tests/test_us084_update_button.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

UI_PATH = REPO_ROOT / "voice_input_app" / "ui.py"
UI_SRC = UI_PATH.read_text(encoding="utf-8")
UI_TREE = ast.parse(UI_SRC)


def _find(name, node_type=(ast.FunctionDef, ast.AsyncFunctionDef), tree=None):
    for node in ast.walk(tree if tree is not None else UI_TREE):
        if isinstance(node, node_type) and node.name == name:
            return node
    return None


def _calls(node):
    """Имена вызванных функций и методов внутри узла."""
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            if isinstance(n.func, ast.Attribute):
                out.add(n.func.attr)
            elif isinstance(n.func, ast.Name):
                out.add(n.func.id)
    return out


def test_auto_check_never_opens_update_screen():
    """AC 1/AC 5: экран обновления открывается только по кнопке."""
    func = _find("on_update_check_result")
    assert func is not None, "пропал обработчик результата проверки обновлений"

    opens = [
        n for n in ast.walk(func)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr in ("open_update_dialog", "download_update")
    ]
    assert opens, "результат проверки вообще не ведёт к обновлению — кнопка стала бесполезной"

    # Каждый показ экрана обязан находиться внутри if manual.
    for call in opens:
        guarded = False
        for node in ast.walk(func):
            if isinstance(node, ast.If) and any(c is call for c in ast.walk(node)):
                test_src = ast.dump(node.test)
                if "manual" in test_src:
                    guarded = True
                    break
        assert guarded, (
            "показ экрана обновления не под условием manual — проверка при запуске "
            "снова откроет окно у всех пользователей (регресс US-084)")

    # Старый безусловный вопрос через QMessageBox.question из этого пути ушёл.
    assert "Скачать и запустить обновление сейчас?" not in UI_SRC, (
        "вернулся автоматический вопрос об обновлении при запуске")


def test_button_moved_to_header():
    """AC 2: кнопка стоит в шапке рядом с версией, в настройках её нет."""
    build = _find("_build_ui")
    assert build is not None
    assigned = {
        n.attr
        for stmt in ast.walk(build)
        if isinstance(stmt, ast.Assign)
        for n in stmt.targets
        if isinstance(n, ast.Attribute)
    }
    assert "header_update_btn" in assigned, "кнопка обновления не создаётся в шапке окна"

    settings = _find("_settings_tab")
    assert settings is not None
    settings_src = ast.dump(settings)
    assert "check_updates_btn" not in settings_src, "кнопка проверки обновлений осталась в настройках"
    assert "check_updates_btn" not in UI_SRC, "в коде остались обращения к старой кнопке настроек"

    # Версия и название остаются ОДНИМ rich-text QLabel: отдельный layout
    # ронял версию с базовой линии (заметка в CLAUDE.md про US-045).
    assert 'title.setTextFormat(Qt.RichText)' in UI_SRC, "версия в шапке перестала быть rich-text"


def test_button_switches_both_ways():
    """AC 3/AC 7: «Обновить» появляется и исчезает без перезапуска."""
    refresh = _find("_refresh_header_update_button")
    assert refresh is not None, "нет метода, приводящего кнопку в шапке к текущему состоянию"
    src = ast.dump(refresh)
    assert "Обновить до" in ast.unparse(refresh), "кнопка не превращается в «Обновить»"
    assert "Проверить обновление" in ast.unparse(refresh), "кнопка не возвращается в исходное состояние"

    result = _find("on_update_check_result")
    assert "_refresh_header_update_button" in _calls(result), "после проверки кнопка не перерисовывается"

    decline = _find("open_update_dialog")
    assert decline is not None, "нет метода открытия экрана обновления"
    assert "_refresh_header_update_button" in _calls(decline), (
        "после отказа кнопка не возвращается в шапку (AC 7)")


def test_periodic_recheck_timer_is_attribute():
    """AC 4 + US-049: повторная проверка есть, её таймер гасится при выходе."""
    assert "_update_recheck_timer" in UI_SRC, "нет повторной проверки обновлений во время работы"
    init = _find("__init__", tree=_find("MainWindow", node_type=ast.ClassDef))
    assert init is not None
    src = ast.unparse(init)
    assert "self._update_recheck_timer" in src, (
        "таймер повторной проверки не атрибут окна — really_quit не погасит его обходом self.__dict__")
    assert ".start()" in src


def test_release_notes_block_is_collapsed():
    """AC 6: «Что нового» свёрнут и разворачивается по нажатию."""
    dialog = _find("UpdateDialog", node_type=ast.ClassDef)
    assert dialog is not None, "нет экрана обновления UpdateDialog"
    src = ast.unparse(dialog)
    assert "Что нового" in src, "на экране обновления нет блока «Что нового»"
    assert "setVisible(False)" in src, "блок «Что нового» не свёрнут по умолчанию"
    assert "setCheckable(True)" in src and "toggled" in src, "блок «Что нового» не разворачивается"
    assert "release_notes" in src, "описание версии не показывается (release_notes не используется)"


def test_policy_hides_header_button():
    """AC 8: при машинной политике кнопки в шапке нет."""
    func = _find("_refresh_updates_policy_state")
    assert func is not None
    src = ast.unparse(func)
    assert "header_update_btn" in src, "политика не управляет кнопкой в шапке"
    assert "setVisible" in src, "кнопка при политике гасится, а не убирается (AC 8)"


def test_centralized_update_untouched():
    """AC 9: US-057/US-058 (централизованное обновление) не задеты."""
    for name in (
        "_check_pending_update",
        "_accept_centralized_update",
        "_decline_centralized_update",
        "_check_idle_update_close",
    ):
        assert _find(name) is not None, "пропал метод централизованного обновления: %s" % name
    assert "update_signal" in UI_SRC, "ui.py больше не работает с маркерами централизованного обновления"


def _run():
    tests = [
        test_auto_check_never_opens_update_screen,
        test_button_moved_to_header,
        test_button_switches_both_ways,
        test_periodic_recheck_timer_is_attribute,
        test_release_notes_block_is_collapsed,
        test_policy_hides_header_button,
        test_centralized_update_untouched,
    ]
    for t in tests:
        t()
        print("PASS: %s" % t.__name__)
    print("US-084 regression: ALL PASS")


if __name__ == "__main__":
    _run()
