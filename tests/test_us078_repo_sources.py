# -*- coding: utf-8 -*-
"""Регресс-тест US-078: обновления берутся из GitHub или GitLab.

Смысл истории: выпуски публикуются не только в GitHub, поэтому сервис должен
определяться по домену ссылки, а не быть зашит в код. Проверки закрепляют это
и то, что настройка, сохранённая старой версией, не теряется.

Проверяет:

1. Разбор ссылки (AC 1, AC 2, AC 5): короткая форма owner/repo = GitHub,
   полные ссылки обоих сервисов, внутренний GitLab, служебные хвосты ссылок.
2. Нераспознанная ссылка даёт понятное сообщение и не ломает проверку (AC 6).
3. Адреса API: GitHub — releases/latest, GitLab — /api/v4/projects/<путь>/releases
   с экранированным путём (AC 2).
4. Установщик GitLab ищется по assets.links, включая ссылку на реестр пакетов;
   архивы исходников установщиком не считаются (AC 3).
5. Результат обоих сервисов одинаков по форме — дальше скачивание и запуск
   общие (AC 4).
6. Отключение обновлений машинной политикой не зависит от сервиса (AC 7).
7. Токен для GitLab не запрашивается и не отправляется (AC 8).

Тест headless и БЕЗ сети: updater._request_json подменяется фейком.
Запуск: python tests/test_us078_repo_sources.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from voice_input_app import updater  # noqa: E402

CURRENT = "4.21.0"

GITHUB_RELEASE = {
    "tag_name": "v4.22.0",
    "html_url": "https://github.com/your-org/voice-input-local/releases/tag/v4.22.0",
    "body": "Что нового в 4.22.0",
    "assets": [
        {"name": "VoiceInputLocalSetup-4.22.0.exe", "browser_download_url": "https://github.example/setup.exe", "size": 111},
        {"name": "VoiceInputLocal-portable.zip", "browser_download_url": "https://github.example/portable.zip", "size": 222},
    ],
}

GITLAB_RELEASE = {
    "tag_name": "v4.22.0",
    "description": "Что нового в 4.22.0",
    "_links": {"self": "https://gitlab.corp.teleofis.ru/tools/voice-input-local/-/releases/v4.22.0"},
    "assets": {
        "count": 3,
        # Архивы исходников — не установщик, их брать нельзя.
        "sources": [
            {"format": "zip", "url": "https://gitlab.corp.teleofis.ru/tools/voice-input-local/-/archive/v4.22.0.zip"},
        ],
        "links": [
            {
                "name": "VoiceInputLocalSetup-4.22.0.exe",
                "url": "https://gitlab.corp.teleofis.ru/tools/voice-input-local/-/releases/v4.22.0/downloads/setup",
                # Ссылка на реестр пакетов — именно так мы публикуем (AC 3).
                "direct_asset_url": "https://gitlab.corp.teleofis.ru/api/v4/projects/42/packages/generic/vil/4.22.0/VoiceInputLocalSetup-4.22.0.exe",
                "link_type": "package",
            },
        ],
    },
}


class _Recorder:
    """Подменяет updater._request_json и запоминает, что у него спросили."""

    def __init__(self, payload):
        self.payload = payload
        self.urls = []
        self.kinds = []

    def __call__(self, url, *, kind=updater.KIND_GITHUB):
        self.urls.append(url)
        self.kinds.append(kind)
        return self.payload


def _with_response(payload, call):
    saved = updater._request_json
    rec = _Recorder(payload)
    try:
        updater._request_json = rec
        return call(), rec
    finally:
        updater._request_json = saved


def test_parse_repo_forms():
    cases = {
        # AC 5: короткая форма, сохранённая старой версией, работает как GitHub.
        "your-org/voice-input-local": (updater.KIND_GITHUB, "github.com", "your-org/voice-input-local"),
        "https://github.com/your-org/voice-input-local": (updater.KIND_GITHUB, "github.com", "your-org/voice-input-local"),
        "https://github.com/your-org/voice-input-local.git": (updater.KIND_GITHUB, "github.com", "your-org/voice-input-local"),
        # AC 1: внутренний GitLab компании.
        "https://gitlab.corp.teleofis.ru/tools/voice-input-local": (
            updater.KIND_GITLAB, "gitlab.corp.teleofis.ru", "tools/voice-input-local"),
        # Вложенные группы GitLab и хвост ссылки на страницу выпусков.
        "https://gitlab.corp.teleofis.ru/group/sub/vil/-/releases": (
            updater.KIND_GITLAB, "gitlab.corp.teleofis.ru", "group/sub/vil"),
        # Внутренний сервис с нетипичным именем — помечается вручную.
        "gitlab:https://git.corp.example.ru/tools/vil": (updater.KIND_GITLAB, "git.corp.example.ru", "tools/vil"),
    }
    for value, expected in cases.items():
        ref = updater.parse_repo(value)
        assert (ref.kind, ref.host, ref.path) == expected, "%r -> %s" % (value, (ref.kind, ref.host, ref.path))

    # Сохранение приводит GitHub к прежней короткой форме — настройка старых
    # версий не переписывается на ровном месте.
    assert updater.normalize_repo("https://github.com/a/b") == "a/b"
    assert updater.normalize_repo("https://gitlab.example.ru/a/b") == "https://gitlab.example.ru/a/b"


def test_unrecognized_link_is_explained():
    for bad in ("https://bitbucket.org/x/y", "просто текст", "https://gitlab.example.ru"):
        ref = updater.parse_repo(bad)
        assert not ref.ok, bad
        assert updater.normalize_repo(bad) == "", bad
        hint = updater.repo_hint(bad)
        assert hint and "не распознана" in hint, hint
    # Пустое значение — не ошибка, а «ещё не настроено».
    assert updater.repo_hint("") and "Укажите ссылку" in updater.repo_hint("")
    assert updater.repo_hint("a/b") == ""

    # AC 6: проверка обновлений не падает молча — она объясняет причину.
    try:
        updater.check_for_update("https://bitbucket.org/x/y", CURRENT)
    except RuntimeError as exc:
        assert "не распознана" in str(exc), exc
    else:
        raise AssertionError("нераспознанная ссылка не вызвала понятной ошибки")
    # Пустая настройка — просто «нечего проверять», без исключения.
    assert updater.check_for_update("", CURRENT) is None


def test_github_request_and_result():
    info, rec = _with_response(GITHUB_RELEASE, lambda: updater.check_for_update("your-org/voice-input-local", CURRENT))
    assert rec.urls == ["https://api.github.com/repos/your-org/voice-input-local/releases/latest"], rec.urls
    assert rec.kinds == [updater.KIND_GITHUB]
    assert info is not None
    assert info.latest_version == "4.22.0"
    assert info.asset_name.endswith(".exe") and info.is_installer
    assert info.release_notes == "Что нового в 4.22.0"


def test_gitlab_request_and_result():
    info, rec = _with_response(
        [GITLAB_RELEASE],  # GitLab отдаёт СПИСОК выпусков, свежий — первый
        lambda: updater.check_for_update("https://gitlab.corp.teleofis.ru/tools/voice-input-local", CURRENT),
    )
    # AC 2: адрес API GitLab с экранированным путём проекта.
    assert rec.urls == [
        "https://gitlab.corp.teleofis.ru/api/v4/projects/tools%2Fvoice-input-local/releases"
    ], rec.urls
    assert rec.kinds == [updater.KIND_GITLAB]
    assert info is not None
    # AC 3: установщик найден по assets.links, адрес — ссылка реестра пакетов.
    assert info.asset_name == "VoiceInputLocalSetup-4.22.0.exe"
    assert "packages/generic" in info.asset_url, info.asset_url
    assert info.is_installer
    # AC 4: форма результата та же, что у GitHub — дальше код общий.
    assert info.latest_version == "4.22.0"
    assert info.release_notes == "Что нового в 4.22.0"
    assert info.current_version == CURRENT


def test_gitlab_sources_are_not_installers():
    """Архив исходников не должен уехать пользователю под видом обновления."""
    only_sources = dict(GITLAB_RELEASE, assets={"sources": GITLAB_RELEASE["assets"]["sources"], "links": []})
    assert updater.gitlab_assets(only_sources) == []
    try:
        _with_response([only_sources], lambda: updater.check_for_update(
            "https://gitlab.corp.teleofis.ru/tools/voice-input-local", CURRENT))
    except RuntimeError as exc:
        assert "нет подходящего файла" in str(exc), exc
    else:
        raise AssertionError("выпуск без установщика принят за пригодный")


def test_older_release_is_not_offered():
    old = dict(GITHUB_RELEASE, tag_name="v4.20.0")
    info, _ = _with_response(old, lambda: updater.check_for_update("a/b", CURRENT))
    assert info is None


def test_no_token_for_gitlab():
    """AC 8: репозитории открытые — токен GitLab не хранится и не шлётся."""
    src = (REPO_ROOT / "voice_input_app" / "updater.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_request_json")
    # Единственное обращение к переменным окружения с токеном — в ветке GitHub.
    token_reads = [
        n for n in ast.walk(func)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and "TOKEN" in n.value
    ]
    assert token_reads, "ветка GitHub перестала подхватывать GITHUB_TOKEN"
    for node in ast.walk(func):
        if isinstance(node, ast.If):
            branch_src = ast.dump(node.test)
            if "KIND_GITHUB" in branch_src:
                break
    else:
        raise AssertionError("в _request_json нет ветвления по сервису — заголовки общие")
    assert "GITLAB_TOKEN" not in src, "появилось хранение токена GitLab — это выходит за рамки US-078"


def test_policy_applies_to_both_services():
    """AC 7: политика отключает апдейтер независимо от сервиса."""
    ui_src = (REPO_ROOT / "voice_input_app" / "ui.py").read_text(encoding="utf-8")
    tree = ast.parse(ui_src)
    func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "check_for_updates")
    first = func.body[0]
    assert isinstance(first, ast.If), "гейт политики больше не первый в check_for_updates"
    names = {n.func.id for n in ast.walk(first.test) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "updates_disabled_by_policy" in names
    # Гейт стоит ДО разбора ссылки, значит сервис на него не влияет.
    body_before_repo = ast.dump(first)
    assert "parse_repo" not in body_before_repo


def _run():
    tests = [
        test_parse_repo_forms,
        test_unrecognized_link_is_explained,
        test_github_request_and_result,
        test_gitlab_request_and_result,
        test_gitlab_sources_are_not_installers,
        test_older_release_is_not_offered,
        test_no_token_for_gitlab,
        test_policy_applies_to_both_services,
    ]
    for t in tests:
        t()
        print("PASS: %s" % t.__name__)
    print("US-078 regression: ALL PASS")


if __name__ == "__main__":
    _run()
