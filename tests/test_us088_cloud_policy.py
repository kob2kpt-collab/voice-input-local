# -*- coding: utf-8 -*-
"""Регресс-тест US-088: машинные политики администратора по работе с облаком.

Закрепляет то, ради чего история заводилась: сотрудник не должен иметь
возможности отправить рабочую запись туда, куда администратор запретил, —
что бы он ни поменял в настройках и в config.json.

Проверяет четыре группы инвариантов:

1. Значения политики трактуются как договорено (AC 2, AC 4, AC 6): три
   состояния ForceInternalModelsOnly, DisableCloud, разбор AllowedEndpoints,
   строгая трактовка заданного, но неразбираемого значения. Реестр не трогаем —
   winreg подменяется фейком (по образцу tests/test_us047_update_policy.py).

2. Приоритет над настройкой пользователя (AC 5): effective_only_internal_models
   возвращает значение политики, а не выбор пользователя, в обе стороны.

3. Статический guard единственной точки чтения (AC 1): ветку
   HKLM\\SOFTWARE\\Policies\\VoiceInputLocal читает только policy.py. Новое
   место чтения = обход политики в новом пути кода, поэтому тест падает.

4. Статический guard чокпоинтов: models._register_cloud_model проверяет
   DisableCloud и AllowedEndpoints, а ui._confirm_cloud_endpoint — политику
   ПЕРВЫМ делом (до сессионных подтверждений US-018).

Тест headless (без Qt). Запуск: python tests/test_us088_cloud_policy.py
"""
from __future__ import annotations

import ast
import os
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from voice_input_app import policy


class _FakeKey:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _install_fake_winreg(query):
    """Подменяет sys.modules['winreg'] фейком; query(name) -> value или raise."""
    fake = types.ModuleType("winreg")
    fake.HKEY_LOCAL_MACHINE = 0
    fake.KEY_READ = 1
    fake.KEY_WOW64_64KEY = 0x100

    def open_key(hive, path, res, access):
        return _FakeKey()

    def query_value_ex(key, name):
        return query(name)

    fake.OpenKey = open_key
    fake.QueryValueEx = query_value_ex
    sys.modules["winreg"] = fake


def _with_policy(values, call):
    """Выполнить call() так, будто в реестре лежат values (dict имя -> значение).

    Значение может быть парой (value, type), исключением или классом исключения.
    Имя, которого в dict нет, отдаётся как отсутствующее (FileNotFoundError).
    """
    saved = sys.modules.get("winreg")

    def query(name):
        if name not in values:
            raise FileNotFoundError()
        item = values[name]
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, type) and issubclass(item, BaseException):
            raise item()
        return item

    try:
        _install_fake_winreg(query)
        return call()
    finally:
        if saved is not None:
            sys.modules["winreg"] = saved
        else:
            sys.modules.pop("winreg", None)


def _skip_if_not_windows() -> bool:
    if os.name != "nt":
        print("SKIP (не Windows): чтение реестра не выполняется, проверены только статические guard-ы")
        return True
    return False


# ─────────────────────────── 1. Значения политики ───────────────────────────


def test_disable_cloud_values():
    if _skip_if_not_windows():
        return
    assert _with_policy({"DisableCloud": (1, 4)}, policy.cloud_disabled) is True
    assert _with_policy({"DisableCloud": (7, 4)}, policy.cloud_disabled) is True
    assert _with_policy({"DisableCloud": (0, 4)}, policy.cloud_disabled) is False
    # Значения нет — политики нет (AC 6).
    assert _with_policy({}, policy.cloud_disabled) is False
    # Значение задано, но не разбирается — строгая сторона: облако запрещено.
    assert _with_policy({"DisableCloud": ("да", 1)}, policy.cloud_disabled) is True
    assert _with_policy({"DisableCloud": OSError("сбой")}, policy.cloud_disabled) is True


def test_force_internal_three_states():
    if _skip_if_not_windows():
        return
    # AC 2: ровно три состояния.
    assert _with_policy({"ForceInternalModelsOnly": (1, 4)}, policy.force_internal_models_only) is True
    assert _with_policy({"ForceInternalModelsOnly": (0, 4)}, policy.force_internal_models_only) is False
    assert _with_policy({}, policy.force_internal_models_only) is None
    # Неразбираемое значение — в строгую сторону (фильтр включён).
    assert _with_policy({"ForceInternalModelsOnly": ("х", 1)}, policy.force_internal_models_only) is True


def test_allowed_endpoints_parsing():
    if _skip_if_not_windows():
        return
    # Значения нет — ограничений нет.
    assert _with_policy({}, policy.allowed_endpoints) is None
    # MULTI_SZ.
    got = _with_policy(
        {"AllowedEndpoints": (["https://foundation-models.api.cloud.ru/v1", "api.elevenlabs.io"], 7)},
        policy.allowed_endpoints,
    )
    assert got == ["https://foundation-models.api.cloud.ru/v1", "api.elevenlabs.io"], got
    # Одна строка с разделителями.
    got = _with_policy({"AllowedEndpoints": ("a.example.com; b.example.com", 1)}, policy.allowed_endpoints)
    assert got == ["a.example.com", "b.example.com"], got
    # Не читается — строгая сторона: не разрешён никто.
    assert _with_policy({"AllowedEndpoints": OSError("сбой")}, policy.allowed_endpoints) == []


def test_endpoint_matching():
    if _skip_if_not_windows():
        return
    allowed = {"AllowedEndpoints": (["https://foundation-models.api.cloud.ru/v1"], 7)}

    def check(url):
        return _with_policy(allowed, lambda: policy.endpoint_allowed(url))

    # Точное совпадение и совпадение без учёта схемы/регистра/хвостового слэша.
    assert check("https://foundation-models.api.cloud.ru/v1") is True
    assert check("http://Foundation-Models.API.cloud.ru/v1/") is True
    # Путь внутри разрешённого адреса разрешён.
    assert check("https://foundation-models.api.cloud.ru/v1/openai") is True
    # Другой хост и «более широкий» адрес — нет: разрешённый путь не открывает хост.
    assert check("https://api.openai.com/v1") is False
    assert check("https://foundation-models.api.cloud.ru") is False
    # Пустой адрес при заданном списке разрешить нельзя.
    assert check("") is False
    # Списка нет — разрешено всё (поведение как сегодня).
    assert _with_policy({}, lambda: policy.endpoint_allowed("https://api.openai.com/v1")) is True


# ────────────────── 2. Приоритет политики над настройкой ────────────────────


def test_policy_beats_user_choice():
    if _skip_if_not_windows():
        return
    force_on = {"ForceInternalModelsOnly": (1, 4)}
    force_off = {"ForceInternalModelsOnly": (0, 4)}

    # AC 5: политика важнее выбора пользователя в ОБЕ стороны.
    assert _with_policy(force_on, lambda: policy.effective_only_internal_models(False)) is True
    assert _with_policy(force_on, lambda: policy.effective_only_internal_models(True)) is True
    assert _with_policy(force_off, lambda: policy.effective_only_internal_models(True)) is False
    assert _with_policy(force_off, lambda: policy.effective_only_internal_models(False)) is False
    # Политики нет — решает пользователь.
    assert _with_policy({}, lambda: policy.effective_only_internal_models(True)) is True
    assert _with_policy({}, lambda: policy.effective_only_internal_models(False)) is False


def test_connection_block_reason():
    if _skip_if_not_windows():
        return

    class _Conn:
        type = "openai"
        base_url = "https://api.openai.com/v1"

    class _Eleven:
        type = "elevenlabs"
        base_url = ""

    # Облако запрещено целиком — причина одна для любого подключения.
    reason = _with_policy({"DisableCloud": (1, 4)}, lambda: policy.connection_block_reason(_Conn()))
    assert "запрещены системным администратором" in reason, reason
    # Адрес вне белого списка — в причине виден сам адрес.
    allowed = {"AllowedEndpoints": (["https://foundation-models.api.cloud.ru/v1"], 7)}
    reason = _with_policy(allowed, lambda: policy.connection_block_reason(_Conn()))
    assert "api.openai.com" in reason, reason
    # ElevenLabs без base_url проверяется по постоянному адресу сервиса,
    # иначе его нельзя было бы ни разрешить, ни запретить осмысленно.
    assert policy.connection_endpoint(_Eleven()) == policy.ELEVENLABS_ENDPOINT
    allow_eleven = {"AllowedEndpoints": (["api.elevenlabs.io"], 7)}
    assert _with_policy(allow_eleven, lambda: policy.connection_block_reason(_Eleven())) == ""
    assert _with_policy(allowed, lambda: policy.connection_block_reason(_Eleven())) != ""
    # Политики нет — не запрещено ничего.
    assert _with_policy({}, lambda: policy.connection_block_reason(_Conn())) == ""


# ─────────── 3. Единственная точка чтения ветки политик (AC 1) ──────────────


def test_policy_key_is_read_only_in_policy_module():
    app_dir = REPO_ROOT / "voice_input_app"
    offenders = []
    for path in sorted(app_dir.glob("*.py")):
        if path.name == "policy.py":
            continue
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        imports_winreg = any(
            (isinstance(n, ast.Import) and any(a.name == "winreg" for a in n.names))
            or (isinstance(n, ast.ImportFrom) and n.module == "winreg")
            for n in ast.walk(tree)
        )
        # Автозапуск ведёт СВОЮ ветку HKCU\...\Run — это не политика.
        mentions_policy_branch = "Policies\\VoiceInputLocal" in src
        if mentions_policy_branch and imports_winreg:
            offenders.append(path.name)
    assert not offenders, (
        "ветку политик читает не только policy.py, а ещё: %s — политику можно обойти новым путём кода"
        % ", ".join(offenders)
    )
    # И обратное: сам модуль политик действительно читает нужную ветку.
    assert policy.POLICY_KEY == r"SOFTWARE\Policies\VoiceInputLocal"


def test_updater_delegates_to_policy():
    """US-047 продолжает работать, но читает реестр через общий модуль (AC 1)."""
    src = (REPO_ROOT / "voice_input_app" / "updater.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    func = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "updates_disabled_by_policy"),
        None,
    )
    assert func is not None, "updates_disabled_by_policy пропала из updater.py"
    calls = {
        n.func.attr
        for n in ast.walk(func)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert "updates_disabled" in calls, "updater больше не делегирует чтение политики модулю policy"


# ───────────────── 4. Чокпоинты, которые нельзя обойти ──────────────────────


def _find_func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def test_register_cloud_model_checks_policy():
    """AC 4/AC 3: чокпоинт регистрации облачных моделей знает про политику."""
    src = (REPO_ROOT / "voice_input_app" / "models.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    func = _find_func(tree, "_register_cloud_model")
    assert func is not None, "не найден чокпоинт _register_cloud_model"
    attrs = {
        n.func.attr
        for n in ast.walk(func)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert "cloud_disabled" in attrs, "чокпоинт не проверяет DisableCloud — облако можно включить мимо политики"
    assert "endpoint_allowed" in attrs, "чокпоинт не проверяет AllowedEndpoints"
    returned = {
        n.value.value
        for n in ast.walk(func)
        if isinstance(n, ast.Return) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)
    }
    assert {"policy-cloud", "policy-endpoint"} <= returned, (
        "чокпоинт не возвращает причины отказа по политике: %s" % sorted(returned))


def test_cloud_confirm_checks_policy_first():
    """Политику нельзя подавить сессионным подтверждением US-018."""
    src = (REPO_ROOT / "voice_input_app" / "ui.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    func = _find_func(tree, "_confirm_cloud_endpoint")
    assert func is not None, "не найден привратник _confirm_cloud_endpoint"
    body = [n for n in func.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    assigns_policy_first = (
        isinstance(body[0], ast.Assign)
        and any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "_cloud_blocked_by_policy"
            for n in ast.walk(body[0])
        )
    )
    assert assigns_policy_first, "первый оператор привратника больше не спрашивает политику"
    assert isinstance(body[1], ast.If), "за проверкой политики не идёт if с отказом"
    assert any(
        isinstance(n, ast.Return) and isinstance(n.value, ast.Constant) and n.value.value is False
        for n in ast.walk(body[1])
    ), "гейт политики не возвращает False (переход на облако не запрещается)"

    # Постобработка и суммаризация тоже спрашивают политику до отправки текста.
    for name in ("_start_dictation_postprocess", "_dispatch_summary"):
        f = _find_func(tree, name)
        assert f is not None, "не найден %s" % name
        attrs = {
            n.func.attr
            for n in ast.walk(f)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        assert "_cloud_blocked_by_policy" in attrs, (
            "%s отправляет текст в облако без проверки машинной политики" % name)


def test_filter_uses_effective_value():
    """ForceInternalModelsOnly действует и в фильтре списков (AC 2, AC 5)."""
    src = (REPO_ROOT / "voice_input_app" / "cloud_placement.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    func = _find_func(tree, "connection_hidden_reason")
    assert func is not None
    names = {n.func.id for n in ast.walk(func) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "connection_only_internal" in names, (
        "фильтр читает поле подключения напрямую — машинная политика обходится правкой config.json")

    cfg_src = (REPO_ROOT / "voice_input_app" / "config.py").read_text(encoding="utf-8")
    assert "effective_only_internal_models" in cfg_src, (
        "у CloudConnection нет действующего значения фильтра с учётом политики")


def _run():
    tests = [
        test_disable_cloud_values,
        test_force_internal_three_states,
        test_allowed_endpoints_parsing,
        test_endpoint_matching,
        test_policy_beats_user_choice,
        test_connection_block_reason,
        test_policy_key_is_read_only_in_policy_module,
        test_updater_delegates_to_policy,
        test_register_cloud_model_checks_policy,
        test_cloud_confirm_checks_policy_first,
        test_filter_uses_effective_value,
    ]
    for t in tests:
        t()
        print("PASS: %s" % t.__name__)
    print("US-088 regression: ALL PASS")


if __name__ == "__main__":
    _run()
