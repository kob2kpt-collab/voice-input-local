# -*- coding: utf-8 -*-
"""Регресс-тест US-047: машинная политика отключает встроенный апдейтер.

Проверяет два инварианта:

1. Функциональный: updater.updates_disabled_by_policy() правильно трактует
   значение реестра HKLM\SOFTWARE\Policies\VoiceInputLocal\DisableUpdates
   (ненулевое → True; ноль/нет значения/ошибка чтения → False; не-Windows →
   False). Реестр не трогаем — winreg подменяется фейком.

2. Статический guard: в ui.py метод check_for_updates ПЕРВЫМ делом проверяет
   политику и делает return (приоритет над cfg.updates_enabled и ручной
   кнопкой). Иначе централизованное отключение можно обойти пользовательской
   настройкой — регресс US-047.

Тест headless (без Qt). Запуск: python tests/test_us047_update_policy.py
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

from voice_input_app import updater


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


def _run_with_winreg(query):
    saved = sys.modules.get("winreg")
    try:
        _install_fake_winreg(query)
        return updater.updates_disabled_by_policy()
    finally:
        if saved is not None:
            sys.modules["winreg"] = saved
        else:
            sys.modules.pop("winreg", None)


def test_policy_value_semantics():
    if os.name != "nt":
        # На не-Windows функция всегда False (ветка os.name != 'nt'), winreg не трогается.
        assert updater.updates_disabled_by_policy() is False
        print("SKIP (не Windows): проверена только ветка non-nt")
        return

    # DisableUpdates = 1 → отключено
    assert _run_with_winreg(lambda name: (1, 4)) is True
    # DisableUpdates = 2 (любое ненулевое) → отключено
    assert _run_with_winreg(lambda name: (2, 4)) is True
    # DisableUpdates = 0 → обычное поведение
    assert _run_with_winreg(lambda name: (0, 4)) is False

    # Значение отсутствует (QueryValueEx бросает FileNotFoundError) → False
    def _missing(name):
        raise FileNotFoundError()

    assert _run_with_winreg(_missing) is False

    # Ошибка чтения реестра (OSError) → fail-open False
    def _oserror(name):
        raise OSError("registry error")

    assert _run_with_winreg(_oserror) is False

    # Нечисловое значение → False
    assert _run_with_winreg(lambda name: ("нет", 1)) is False


def _find_func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def test_gate_is_first_in_check_for_updates():
    ui_src = (REPO_ROOT / "voice_input_app" / "ui.py").read_text(encoding="utf-8")
    tree = ast.parse(ui_src)

    func = _find_func(tree, "check_for_updates")
    assert func is not None, "не найден метод check_for_updates в ui.py"

    first = func.body[0]
    assert isinstance(first, ast.If), "первый оператор check_for_updates — не if (гейт политики сместился/удалён)"

    called = {
        n.func.id
        for n in ast.walk(first.test)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "updates_disabled_by_policy" in called, "первый if не проверяет updates_disabled_by_policy — приоритет политики потерян"
    assert any(isinstance(x, ast.Return) for x in ast.walk(first)), "гейт политики не делает return"

    # Метод обновления состояния UI и импорт хелпера на месте.
    assert _find_func(tree, "_refresh_updates_policy_state") is not None, "нет _refresh_updates_policy_state"
    assert "updates_disabled_by_policy" in ui_src, "ui.py не импортирует/не использует updates_disabled_by_policy"


def _run():
    tests = [
        test_policy_value_semantics,
        test_gate_is_first_in_check_for_updates,
    ]
    for t in tests:
        t()
        print(f"PASS: {t.__name__}")
    print("US-047 regression: ALL PASS")


if __name__ == "__main__":
    _run()
