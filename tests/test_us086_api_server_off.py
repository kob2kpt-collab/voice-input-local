# -*- coding: utf-8 -*-
r"""Регресс-тесты US-086: REST API-сервер выключен в поставке, код сохранён.

Решение владельца продукта от 10.09.2026: сервер не запускается и не виден
пользователю, но код остаётся в проекте ради будущего серверного режима
(US-087). Тест закрепляет обе половины обещания.

Что проверяется:

1. Рубильник `voice_input_app/features.py` выключен.
2. Внутри `_start_api_server` стоит собственный ранний выход — новый вызов
   метода не поднимет сервер мимо рубильника.
3. Любое обращение к `_start_api_server` и `_api_tab` в ui.py находится под
   `if` с рубильником.
4. Подключение автосохранения переживает отсутствие вкладки: без `hasattr`
   приложение падало бы на старте у всех пользователей.
5. В `requirements.txt` нет незакомментированных fastapi/uvicorn/
   python-multipart, а `build_exe.bat` исключает их из сборки — и НЕ исключает
   anyio/h11/httpx/httpcore/pydantic/click, которыми пользуется загрузка
   моделей.
6. Путь возврата цел: модуль api_server.py, страница swagger.html, поля api_*
   в config.py и методы интерфейса на месте, а верхний уровень api_server.py
   не импортирует веб-зависимости (иначе модуль перестал бы импортироваться
   после чистки сборки).

Тест headless, Qt не поднимается. Запуск (в venv приложения):
    .venv\Scripts\python.exe tests\test_us086_api_server_off.py
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from voice_input_app.features import API_SERVER_ENABLED  # noqa: E402

PACKAGE_DIR = REPO_ROOT / "voice_input_app"
UI_PATH = PACKAGE_DIR / "ui.py"
CONFIG_PATH = PACKAGE_DIR / "config.py"
API_PATH = PACKAGE_DIR / "api_server.py"
REQUIREMENTS = REPO_ROOT / "requirements.txt"
BUILD_BAT = REPO_ROOT / "build_exe.bat"

FLAG = "API_SERVER_ENABLED"
# Точки входа в функциональность REST API: запуск сервера и создание вкладки.
GUARDED_NAMES = ("_start_api_server", "_api_tab")
# Веб-сервер в поставку не кладём.
EXCLUDED_MODULES = ("fastapi", "uvicorn", "starlette", "multipart", "python_multipart")
# А это трогать нельзя: их тянет huggingface_hub, иначе сломается скачивание моделей.
KEPT_MODULES = ("anyio", "h11", "httpx", "httpcore", "pydantic", "click")


def _ui_tree() -> ast.Module:
    return ast.parse(UI_PATH.read_text(encoding="utf-8"))


def _mentions_flag(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Name) and n.id == FLAG for n in ast.walk(node))


def _method_body(path: Path, header: str) -> str:
    src = path.read_text(encoding="utf-8")
    start = src.index(header)
    end = src.find("\n    def ", start + len(header))
    return src[start: end if end != -1 else len(src)]


def test_switch_is_off() -> None:
    """Рубильник поставки выключен."""
    assert API_SERVER_ENABLED is False, (
        "API_SERVER_ENABLED снова True: REST API вернулся в поставку. Если это намеренно, "
        "выполните чек-лист возврата из voice_input_app/features.py и обновите этот тест."
    )


def test_start_api_server_returns_early() -> None:
    """Внутри метода стоит собственный гейт — вызов мимо рубильника не поднимет сервер."""
    for node in ast.walk(_ui_tree()):
        if isinstance(node, ast.FunctionDef) and node.name == "_start_api_server":
            body = [
                stmt for stmt in node.body
                if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))
            ]
            assert body, "тело _start_api_server пустое"
            first = body[0]
            assert isinstance(first, ast.If) and _mentions_flag(first.test), (
                "первым оператором _start_api_server должен быть гейт по " + FLAG
            )
            assert any(isinstance(stmt, ast.Return) for stmt in first.body), (
                "гейт в _start_api_server не выходит из метода"
            )
            return
    raise AssertionError("метод _start_api_server не найден в ui.py")


def test_every_api_entry_point_is_guarded() -> None:
    """Запуск сервера и создание вкладки возможны только под рубильником."""
    unguarded: list[str] = []

    def walk(node: ast.AST, guarded: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Attribute) and child.attr in GUARDED_NAMES and not guarded:
                unguarded.append("%s (строка %d)" % (child.attr, child.lineno))
            if isinstance(child, ast.If):
                walk(child.test, guarded)
                inner = guarded or _mentions_flag(child.test)
                for stmt in child.body:
                    walk(stmt, inner)
                for stmt in child.orelse:
                    walk(stmt, guarded)
                continue
            walk(child, guarded)

    walk(_ui_tree(), False)
    assert not unguarded, (
        "обращение к REST API мимо рубильника " + FLAG + ": " + ", ".join(unguarded)
    )


def test_autosave_hookup_survives_missing_tab() -> None:
    """Виджетов вкладки нет — без hasattr приложение падало бы на старте у всех."""
    body = _method_body(UI_PATH, "    def _connect_settings_autosave(")
    assert 'hasattr(self, "api_enabled_check")' in body, (
        "подключение автосохранения обращается к виджетам вкладки «API» без проверки hasattr"
    )


def test_requirements_do_not_ship_web_server() -> None:
    """Зависимости веб-сервера не устанавливаются, пока функция выключена."""
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        assert not re.match(r"^(fastapi|uvicorn|python-multipart)\b", stripped, re.I), (
            "в requirements.txt снова активна зависимость веб-сервера: " + stripped
        )


def test_build_excludes_web_server_and_keeps_model_download() -> None:
    """Сборка не кладёт веб-сервер в exe и не задевает загрузку моделей."""
    bat = BUILD_BAT.read_text(encoding="utf-8")
    for module in EXCLUDED_MODULES:
        assert "--exclude-module " + module in bat, (
            "build_exe.bat не исключает " + module + " из сборки"
        )
    for module in KEPT_MODULES:
        assert "--exclude-module " + module not in bat, (
            "исключение " + module + " сломает скачивание моделей (его тянет huggingface_hub)"
        )


def test_way_back_is_intact() -> None:
    """Путь возврата цел: код, страница Swagger и настройки на месте."""
    assert API_PATH.exists(), "удалён voice_input_app/api_server.py — возврат функции невозможен"
    assert (REPO_ROOT / "swagger.html").exists(), "удалена страница swagger.html"
    config = CONFIG_PATH.read_text(encoding="utf-8")
    for field in ("api_enabled", "api_host", "api_port", "api_key"):
        assert re.search(r"^\s+" + field + r":", config, re.M), (
            "из config.py пропало поле " + field + " — настройки пользователя потеряются"
        )
    ui = UI_PATH.read_text(encoding="utf-8")
    for method in ("def _api_tab(", "def _start_api_server(", "def open_swagger("):
        assert method in ui, "из ui.py пропал метод: " + method


def test_api_server_module_has_no_top_level_web_imports() -> None:
    """fastapi и uvicorn импортируются внутри функций — модуль импортируется и без них."""
    tree = ast.parse(API_PATH.read_text(encoding="utf-8"))
    for node in tree.body:
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        for name in names:
            root = name.split(".")[0]
            assert root not in EXCLUDED_MODULES, (
                "api_server.py импортирует " + root + " на верхнем уровне: после чистки сборки "
                "модуль перестанет импортироваться"
            )


def _run() -> None:
    tests = [
        test_switch_is_off,
        test_start_api_server_returns_early,
        test_every_api_entry_point_is_guarded,
        test_autosave_hookup_survives_missing_tab,
        test_requirements_do_not_ship_web_server,
        test_build_excludes_web_server_and_keeps_model_download,
        test_way_back_is_intact,
        test_api_server_module_has_no_top_level_web_imports,
    ]
    for test in tests:
        test()
        print("PASS: " + test.__name__)
    print("US-086 regression: ALL PASS")


if __name__ == "__main__":
    _run()
