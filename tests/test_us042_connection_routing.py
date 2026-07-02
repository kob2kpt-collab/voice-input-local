# -*- coding: utf-8 -*-
"""Регресс-тест US-042: подключение диктовки не наследует адрес чужого провайдера.

Проверяет два инварианта:

1. Функциональный: разрешение облачного подключения по ключу модели берёт
   base_url/api_key ИМЕННО того подключения, чей id зашит в ключе, независимо
   от значений устаревших глобальных полей cfg.openai_stt_base_url/api_key.

2. Статический guard: метод MainWindow._sync_legacy_fields_to_connections
   (обезврежен в US-042) больше не содержит присваиваний connection.base_url /
   connection.api_key — иначе баг US-042 вернётся.

Тест headless (без Qt): импортирует только config и models + парсит ui.py как
текст. Запуск: python tests/test_us042_connection_routing.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from voice_input_app.config import AppConfig, CloudConnection, CONNECTION_TYPE_OPENAI
from voice_input_app.models import resolve_cloud_connection

CLOUDRU_URL = "https://foundation-models.api.cloud.ru/v1"
GROQ_URL = "https://api.groq.com/openai/v1"


def _make_cfg(conns, legacy_url, legacy_key):
    cfg = AppConfig()
    cfg.cloud_connections = list(conns)
    # Легаси-поля намеренно указывают на ОДНО из подключений (источник бага US-042).
    cfg.openai_stt_base_url = legacy_url
    cfg.openai_stt_api_key = legacy_key
    return cfg


def test_resolution_uses_connection_not_legacy():
    a = CloudConnection(type=CONNECTION_TYPE_OPENAI, base_url=CLOUDRU_URL, api_key="KEY_A", name="Cloud.ru")
    b = CloudConnection(type=CONNECTION_TYPE_OPENAI, base_url=GROQ_URL, api_key="KEY_B", name="Groq")

    # Легаси указывает на A (Cloud.ru), а для диктовки выбрано B (Groq).
    cfg = _make_cfg([a, b], legacy_url=CLOUDRU_URL, legacy_key="KEY_A")
    key_b = f"cloud:{b.id}:whisper-large-v3"
    conn = resolve_cloud_connection(cfg, key_b)
    assert conn is b, "резолв вернул не то подключение"
    assert conn.base_url == GROQ_URL, f"base_url подменён легаси-полем: {conn.base_url!r}"
    assert conn.api_key == "KEY_B", f"api_key подменён легаси-полем: {conn.api_key!r}"

    # Зеркальный сценарий: легаси = Groq, выбран Cloud.ru.
    cfg2 = _make_cfg([a, b], legacy_url=GROQ_URL, legacy_key="KEY_B")
    key_a = f"cloud:{a.id}:openai/whisper-large-v3"
    conn2 = resolve_cloud_connection(cfg2, key_a)
    assert conn2 is a and conn2.base_url == CLOUDRU_URL, "зеркальный резолв подменён легаси-полем"


def test_single_connection_still_resolves():
    a = CloudConnection(type=CONNECTION_TYPE_OPENAI, base_url=CLOUDRU_URL, api_key="KEY_A", name="Cloud.ru")
    cfg = _make_cfg([a], legacy_url=CLOUDRU_URL, legacy_key="KEY_A")
    conn = resolve_cloud_connection(cfg, f"cloud:{a.id}:openai/whisper-large-v3")
    assert conn is a and conn.base_url == CLOUDRU_URL


def test_shim_does_not_write_connection_fields():
    """Статический guard: тело _sync_legacy_fields_to_connections не должно
    присваивать base_url/api_key объектам подключений (иначе US-042 вернётся)."""
    ui_src = (REPO_ROOT / "voice_input_app" / "ui.py").read_text(encoding="utf-8")
    tree = ast.parse(ui_src)
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_sync_legacy_fields_to_connections":
            target = node
            break
    if target is None:
        # US-043: шим полностью удалён — идеальный исход, писать нечему.
        return
    bad = []
    for n in ast.walk(target):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Attribute) and t.attr in ("base_url", "api_key"):
                    bad.append(getattr(n, "lineno", "?"))
    assert not bad, f"шим снова пишет base_url/api_key подключения (строки {bad}) — регресс US-042"


def _run():
    tests = [
        test_resolution_uses_connection_not_legacy,
        test_single_connection_still_resolves,
        test_shim_does_not_write_connection_fields,
    ]
    for t in tests:
        t()
        print(f"PASS: {t.__name__}")
    print("US-042 regression: ALL PASS")


if __name__ == "__main__":
    _run()
