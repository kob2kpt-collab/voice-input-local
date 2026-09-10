# -*- coding: utf-8 -*-
"""Регресс-тест US-089: состав релиза закреплён и проверяем.

Смысл истории: служба информационной безопасности допускает программу на
рабочие машины по проверяемому составу, а не по обещанию разработчика. Значит
закрепление должно быть невозможно потерять по невнимательности — отсюда
статические проверки замка, установщика зависимостей и релизного сценария.

Проверяет:

1. Замок существует, все версии закреплены `==` и у каждой есть хэш (AC 1).
2. Всё, что просит requirements.txt, есть в замке (AC 1), и сам замок читается
   любым средством анализа состава — в нём только ASCII.
3. install.ps1 ставит из замка с проверкой хэшей и НЕ откатывается на
   диапазоны молча (AC 2).
4. В релизном сценарии закреплены версии Python и Inno Setup, а действия
   GitHub Actions — по SHA, а не по плавающему тегу (AC 3).
5. Тесты прогоняются ДО сборки и их падение останавливает выпуск (AC 4).
6. К выпуску прикладываются SBOM и отчёт проверки уязвимостей (AC 5, AC 6).
7. Порядок обновления закреплённых версий описан в документации (AC 7).

Тест headless и БЕЗ сети. Запуск: python tests/test_us089_release_composition.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

LOCKFILE = REPO_ROOT / "requirements.lock.txt"
REQUIREMENTS = REPO_ROOT / "requirements.txt"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
INSTALL_PS1 = REPO_ROOT / "install.ps1"
DOCS = REPO_ROOT / "docs" / "enterprise-deploy.md"

PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\]+)")


def canonical(name: str) -> str:
    return "".join("-" if ch in "-_." else ch for ch in name.lower())


def read_lock_pins() -> dict[str, list[str]]:
    """Имя пакета -> список его хэшей."""
    pins: dict[str, list[str]] = {}
    current = None
    for raw in LOCKFILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = PIN_RE.match(line)
        if match:
            current = canonical(match.group(1))
            pins[current] = []
        elif current is None:
            raise AssertionError("строка замка вне пина: %r" % line)
        if current is not None:
            pins[current].extend(re.findall(r"--hash=sha256:([0-9a-f]{64})", line))
    return pins


def test_lock_exists_and_is_pinned():
    assert LOCKFILE.exists(), "нет requirements.lock.txt — состав выпуска не закреплён"
    pins = read_lock_pins()
    assert len(pins) >= 20, "в замке подозрительно мало пакетов: %d" % len(pins)
    without_hash = [name for name, hashes in pins.items() if not hashes]
    assert not without_hash, (
        "пины без хэша: %s — pip откажется ставить замок целиком в режиме --require-hashes"
        % ", ".join(sorted(without_hash)))
    # Диапазонов в замке быть не должно: он источник факта, а не намерения.
    text = LOCKFILE.read_text(encoding="utf-8")
    for bad in (">=", "<=", "~=", ">", "<"):
        offenders = [
            line for line in text.splitlines()
            if bad in line and not line.strip().startswith("#")
        ]
        assert not offenders, "в замке остался диапазон версий: %r" % offenders[:2]


def test_lock_covers_requirements():
    pins = read_lock_pins()
    missing = []
    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name = re.split(r"[<>=!\[;]", line, 1)[0].strip()
        if not name:
            continue
        if canonical(name) not in pins:
            missing.append(name)
    assert not missing, "в замке нет пакетов из requirements.txt: %s" % ", ".join(missing)


def test_lock_is_ascii_only():
    """Замок читают ЧУЖИЕ средства, а они декодируют его по локали.

    pip_requirements_parser (его использует pip-audit) читает requirements-файл
    в кодировке системы: под русской Windows это cp1251, и кириллица в
    комментарии роняет анализатор UnicodeDecodeError. Мы обещали службе ИБ
    ПРОВЕРЯЕМЫЙ состав — значит файл обязан читаться без PYTHONUTF8 и прочих
    переменных окружения. Отсюда единственное место в проекте, где комментарии
    пишутся по-английски.
    """
    raw = LOCKFILE.read_bytes()
    non_ascii = sorted({b for b in raw if b > 127})
    assert not non_ascii, (
        "в замке есть не-ASCII байты %s — сторонние анализаторы состава не прочитают файл "
        "на машине с не-UTF-8 локалью" % non_ascii[:8])
    # И генератор не должен снова начать писать шапку по-русски.
    generator = (REPO_ROOT / "scripts" / "make_lockfile.py").read_text(encoding="utf-8")
    header = generator.split("lines = [", 1)[1].split("]", 1)[0]
    bad = sorted({ch for ch in header if ord(ch) > 127})
    assert not bad, "шапка замка в make_lockfile.py снова содержит не-ASCII: %s" % bad


def test_install_uses_lock():
    """AC 2: установка из исходников даёт тот же состав, что и релиз."""
    src = INSTALL_PS1.read_text(encoding="utf-8")
    assert "requirements.lock.txt" in src, "install.ps1 не знает про закреплённый состав"
    assert "--require-hashes" in src, "install.ps1 ставит замок без проверки хэшей"
    # Молчаливого отката на диапазоны при ОШИБКЕ установки быть не должно.
    assert "exit 1" in src.split("--require-hashes", 1)[1][:600], (
        "после неудачной установки из замка install.ps1 продолжает работу — состав разойдётся с релизом")


def test_workflow_pins_toolchain_and_actions():
    """AC 3: версии инструментов и действий закреплены."""
    src = WORKFLOW.read_text(encoding="utf-8")

    # Действия — по SHA (40 hex), а не по тегу v5/v6.
    uses = re.findall(r"uses:\s*(\S+)", src)
    assert uses, "в релизном сценарии нет ни одного действия"
    for ref in uses:
        assert "@" in ref, "действие без версии: %s" % ref
        _, _, version = ref.partition("@")
        assert re.fullmatch(r"[0-9a-f]{40}", version), (
            "действие %s закреплено тегом, а не SHA: тег можно передвинуть на другой код" % ref)

    # Версии Python и Inno Setup — точные, без «latest».
    python_version = re.search(r'PYTHON_VERSION:\s*"([^"]+)"', src)
    assert python_version, "версия Python не закреплена"
    assert re.fullmatch(r"\d+\.\d+\.\d+", python_version.group(1)), (
        "версия Python задана неточно: %s" % python_version.group(1))
    inno = re.search(r'INNOSETUP_VERSION:\s*"([^"]+)"', src)
    assert inno, "версия Inno Setup не закреплена"
    assert re.fullmatch(r"\d+(\.\d+)+", inno.group(1)), "версия Inno Setup задана неточно"
    assert "--version=" in src, "Inno Setup ставится без указания версии"
    assert "runs-on: windows-latest" not in src, "образ раннера не закреплён (windows-latest плавает)"

    # Зависимости ставятся из замка.
    assert "--require-hashes -r requirements.lock.txt" in src, "релиз ставит зависимости не из замка"


def test_workflow_runs_tests_before_build():
    """AC 4: упавшие тесты останавливают выпуск."""
    src = WORKFLOW.read_text(encoding="utf-8")
    assert "scripts\\run_tests.py" in src, "в релизном сценарии не прогоняются тесты"
    tests_at = src.index("scripts\\run_tests.py")
    build_at = src.index("build_exe.bat")
    assert tests_at < build_at, "тесты прогоняются ПОСЛЕ сборки — смысл гейта теряется"

    # У шага с тестами не должно быть continue-on-error: иначе падение не блокирует.
    step = src[src.rindex("- name:", 0, tests_at):tests_at]
    assert "continue-on-error" not in step, (
        "шаг с тестами помечен continue-on-error — падение теста не остановит публикацию")


def test_workflow_attaches_sbom_and_audit():
    """AC 5, AC 6: перечень состава и отчёт об уязвимостях доступны."""
    src = WORKFLOW.read_text(encoding="utf-8")
    assert "scripts\\make_sbom.py" in src, "SBOM к выпуску не собирается"
    assert "pip_audit" in src or "pip-audit" in src, "проверка зависимостей на уязвимости не выполняется"
    assert "upload-artifact" in src, "отчёты не сохраняются как артефакты сборки"
    publish = src[src.index("Publish GitHub Release"):]
    assert "sbom-" in publish, "SBOM не прикладывается к выпуску"
    assert "pip-audit-" in publish, "отчёт об уязвимостях не прикладывается к выпуску"


def test_scripts_exist():
    for name in ("make_lockfile.py", "make_sbom.py", "make_third_party.py", "run_tests.py"):
        assert (REPO_ROOT / "scripts" / name).exists(), "нет scripts/%s" % name


def test_update_procedure_documented():
    """AC 7: как обновлять закреплённые версии — написано."""
    src = DOCS.read_text(encoding="utf-8")
    assert "requirements.lock.txt" in src, "документация не описывает закреплённый состав"
    assert "make_lockfile.py" in src, "не описано, чем пересобирается замок"
    assert "Как обновить закреплённые версии" in src, "нет порядка обновления закреплённых версий"


def _run():
    tests = [
        test_lock_exists_and_is_pinned,
        test_lock_covers_requirements,
        test_lock_is_ascii_only,
        test_install_uses_lock,
        test_workflow_pins_toolchain_and_actions,
        test_workflow_runs_tests_before_build,
        test_workflow_attaches_sbom_and_audit,
        test_scripts_exist,
        test_update_procedure_documented,
    ]
    for t in tests:
        t()
        print("PASS: %s" % t.__name__)
    print("US-089 regression: ALL PASS")


if __name__ == "__main__":
    _run()
