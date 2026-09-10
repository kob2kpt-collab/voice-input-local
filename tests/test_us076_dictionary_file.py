# -*- coding: utf-8 -*-
"""Регресс-тест US-076: словарь хранится и передаётся отдельным файлом.

Смысл истории: готовые настройки раздают коллегам подменой config.json, и
словарь, собранный человеком под себя, не должен при этом теряться. Отсюда все
проверки ниже — они закрепляют ровно это поведение, а не «файл сохранился».

Проверяет:

1. Круговорот запись/чтение и разбор «голого» списка записей.
2. Отсутствующий и повреждённый файл не мешают работе (AC 5), повреждённый
   при этом НЕ перезаписывается — иначе одна опечатка в JSON стоила бы
   человеку всех терминов.
3. Перенос из config.json (AC 2): записи переезжают, повтор не дублирует.
4. Подмена файла настроек не меняет словарь (AC 3).
5. Слияние при импорте: записи человека остаются его (AC 4).
6. Статические guard-ы: поля postprocess_glossary в настройках больше нет
   (AC 1), а выключатель словаря остался (AC 6).

Тест headless (без Qt), работает на временном профиле — настройки и словарь
пользователя не трогает. Запуск: python tests/test_us076_dictionary_file.py
"""
from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# ВАЖНО: временный профиль до импорта модулей — иначе тест писал бы в
# %LOCALAPPDATA%\VoiceInputLocal реального пользователя.
_PROFILE = Path(tempfile.mkdtemp(prefix="vil-us076-"))
os.environ["LOCALAPPDATA"] = str(_PROFILE)

from voice_input_app import glossary  # noqa: E402
from voice_input_app.paths import config_path, dictionary_path  # noqa: E402


def _reset_profile() -> None:
    for p in (dictionary_path(), config_path()):
        if p.exists():
            p.unlink()


ENTRY = {"term": "Телеофис", "distortions": "телеофис, телофис", "context": "название компании", "exclusions": ""}


def test_roundtrip_and_shapes():
    _reset_profile()
    assert glossary.load().status == glossary.STATUS_MISSING

    glossary.save([ENTRY])
    result = glossary.load()
    assert result.status == glossary.STATUS_OK
    assert len(result.entries) == 1
    got = result.entries[0]
    assert got["term"] == "Телеофис"
    # Запись без явного enabled считается включённой (совместимость с US-044).
    assert got["enabled"] is True

    # Файл читается и как «голый» список — так проще принять словарь коллеги.
    dictionary_path().write_text(json.dumps([ENTRY], ensure_ascii=False), encoding="utf-8")
    assert len(glossary.load().entries) == 1

    # Записи без термина отбрасываются: подставлять по ним нечего.
    assert glossary.normalize_entries([{"distortions": "что-то"}, ENTRY, "мусор", 42]) == [
        dict(ENTRY, enabled=True)
    ]


def test_missing_and_broken_file_do_not_break_start():
    _reset_profile()
    # Файла нет — пустой словарь, без исключения (AC 5).
    result = glossary.load()
    assert result.entries == [] and result.ok

    # Файл повреждён — пустой словарь, признак broken, файл НЕ перезаписан.
    broken = "{это не json"
    dictionary_path().write_text(broken, encoding="utf-8")
    result = glossary.load()
    assert result.status == glossary.STATUS_BROKEN, result.status
    assert result.entries == []
    assert result.ok is False
    assert dictionary_path().read_text(encoding="utf-8") == broken, "повреждённый файл словаря молча перезаписан"


def test_migration_from_config():
    _reset_profile()
    # Старый config.json со словарём внутри (как было до US-076).
    config_path().write_text(
        json.dumps({"postprocess_glossary": [ENTRY], "postprocess_glossary_enabled": True}, ensure_ascii=False),
        encoding="utf-8",
    )
    moved = glossary.migrate_from_config()
    assert moved == 1, moved
    assert len(glossary.load().entries) == 1

    # AC 2: повторный перенос не дублирует записи.
    again = glossary.migrate_from_config()
    assert again == 0, again
    assert len(glossary.load().entries) == 1

    # Резервная копия настроек до переноса сохранена.
    assert config_path().with_suffix(".pre-dictionary.bak").exists()


def test_config_replacement_keeps_user_dictionary():
    """AC 3: подложили чужой файл настроек — словарь остался пользовательским."""
    _reset_profile()
    glossary.save([ENTRY])  # словарь человека
    # Коллега прислал config.json со СВОИМ словарём внутри.
    config_path().write_text(
        json.dumps({"postprocess_glossary": [{"term": "ЧужойТермин"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    assert glossary.migrate_from_config() == 0
    terms = [e["term"] for e in glossary.load().entries]
    assert terms == ["Телеофис"], terms


def test_merge_keeps_user_entries():
    mine = [dict(ENTRY, context="мой контекст")]
    theirs = [
        {"term": "телеофис", "context": "их контекст"},   # тот же термин в другом регистре
        {"term": "SIM-инжектор", "context": "их термин"},  # новый
    ]
    merged = glossary.merge_entries(mine, theirs)
    assert [e["term"] for e in merged] == ["Телеофис", "SIM-инжектор"], merged
    assert merged[0]["context"] == "мой контекст", "импорт затёр запись пользователя"


def test_export_import_roundtrip():
    _reset_profile()
    out = _PROFILE / "shared-dictionary.json"
    count = glossary.export_to(out, [ENTRY])
    assert count == 1 and out.exists()
    result = glossary.import_from(out)
    assert result.ok and len(result.entries) == 1


# ────────────────────────── статические guard-ы ─────────────────────────────


def test_config_has_no_glossary_field():
    """AC 1: словарь больше не поле настроек; выключатель (AC 6) остался."""
    src = (REPO_ROOT / "voice_input_app" / "config.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fields = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "AppConfig":
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    fields.add(stmt.target.id)
    assert "postprocess_glossary" not in fields, "словарь снова хранится в config.json — подмена настроек затрёт его"
    assert "postprocess_glossary_enabled" in fields, "пропал выключатель словаря (AC 6)"


def test_glossary_read_through_store():
    """Единственный путь словаря в облачный запрос — хранилище, не настройки."""
    workers_src = (REPO_ROOT / "voice_input_app" / "workers.py").read_text(encoding="utf-8")
    assert "glossary_store.load_entries()" in workers_src, "воркер постобработки больше не читает словарь из файла"
    assert 'getattr(self.cfg, "postprocess_glossary"' not in workers_src, "воркер читает словарь из настроек"
    # Мастер-гейт US-046 остался там же, где и был.
    assert "postprocess_glossary_enabled" in workers_src, "пропал мастер-гейт словаря в воркере"

    ui_src = (REPO_ROOT / "voice_input_app" / "ui.py").read_text(encoding="utf-8")
    assert "self.cfg.postprocess_glossary =" not in ui_src, "интерфейс снова пишет словарь в настройки"
    for name in ("_on_dictionary_export", "_on_dictionary_import"):
        assert "def %s" % name in ui_src, "нет кнопки словаря: %s (AC 4)" % name
    assert "glossary_store.migrate_from_config()" in ui_src, "перенос словаря не вызывается при старте (AC 2)"


def _run():
    tests = [
        test_roundtrip_and_shapes,
        test_missing_and_broken_file_do_not_break_start,
        test_migration_from_config,
        test_config_replacement_keeps_user_dictionary,
        test_merge_keeps_user_entries,
        test_export_import_roundtrip,
        test_config_has_no_glossary_field,
        test_glossary_read_through_store,
    ]
    for t in tests:
        t()
        print("PASS: %s" % t.__name__)
    print("US-076 regression: ALL PASS")


if __name__ == "__main__":
    _run()
