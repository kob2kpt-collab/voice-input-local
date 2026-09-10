# -*- coding: utf-8 -*-
"""Регресс-тест US-090: лицензия, контакт по безопасности, чужие лицензии.

Смысл истории: служба информационной безопасности согласует развёртывание,
только когда видно, на каком основании программа раздаётся сотрудникам, куда
сообщать об уязвимости и какие чужие компоненты внутри. Проверки закрепляют
наличие этих трёх документов и то, что они не шаблонные и не рукописные.

Проверяет:

1. Файл лицензии есть, назван правообладатель, тип указан в README (AC 1).
2. SECURITY.md заполнен: адрес, сроки, реальные версии; шаблон GitHub убран (AC 2).
3. Перечень чужих лицензий есть, тексты LGPL/GPL приложены (AC 3).
4. Лицензия и перечень попадают в поставку установщиком (AC 4).
5. Перечень собирается из закреплённого состава, а не руками (AC 5).

Тест headless и БЕЗ сети. Запуск: python tests/test_us090_license_and_notices.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

LICENSE = REPO_ROOT / "LICENSE"
SECURITY = REPO_ROOT / "SECURITY.md"
THIRD_PARTY = REPO_ROOT / "THIRD-PARTY-LICENSES.md"
LICENSES_DIR = REPO_ROOT / "licenses"
INSTALLER = REPO_ROOT / "installer" / "VoiceInputLocal.iss"
README = REPO_ROOT / "README.md"

# Шаблонные фразы GitHub: если хоть одна осталась, файл не заполняли.
GITHUB_TEMPLATE_MARKERS = (
    "Use this section to tell people",
    "Tell them where to go, how often",
    "| 5.1.x",
)


def test_license_present():
    assert LICENSE.exists(), "нет файла лицензии — непонятно, на каком основании раздаётся программа"
    text = LICENSE.read_text(encoding="utf-8")
    assert len(text) > 600, "файл лицензии подозрительно короткий"
    assert "Телеофис" in text, "в лицензии не назван правообладатель"
    assert "THIRD-PARTY-LICENSES.md" in text, "лицензия не отсылает к перечню чужих компонентов"
    # Тип лицензии виден в README (AC 1).
    readme = README.read_text(encoding="utf-8")
    assert "LICENSE" in readme, "README не упоминает файл лицензии"


def test_security_md_is_filled_in():
    text = SECURITY.read_text(encoding="utf-8")
    for marker in GITHUB_TEMPLATE_MARKERS:
        assert marker not in text, "в SECURITY.md остался шаблонный текст GitHub: %r" % marker
    assert re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text), (
        "в SECURITY.md нет адреса для сообщений об уязвимостях")
    assert "рабочих дней" in text, "в SECURITY.md не указан ожидаемый срок ответа"

    # Поддерживаемые версии должны соответствовать реальной версии программы.
    init = (REPO_ROOT / "voice_input_app" / "__init__.py").read_text(encoding="utf-8")
    version = re.search(r'__version__\s*=\s*"(\d+)\.(\d+)\.', init)
    assert version, "не удалось прочитать версию программы"
    line = "%s.%s.x" % (version.group(1), version.group(2))
    assert line in text, (
        "в SECURITY.md нет текущей линии версий %s — таблица поддерживаемых версий устарела" % line)


def test_third_party_list_present():
    assert THIRD_PARTY.exists(), "нет перечня чужих компонентов"
    text = THIRD_PARTY.read_text(encoding="utf-8")
    # AC 5: собирается из закреплённого состава, а не пишется руками.
    assert "requirements.lock.txt" in text, "перечень не сослан на закреплённый состав"
    assert "make_third_party.py" in text, "не указано, чем пересобирается перечень"
    rows = [line for line in text.splitlines() if line.startswith("| ") and line.count("|") >= 4]
    assert len(rows) >= 20, "в перечне подозрительно мало компонентов: %d" % len(rows)
    for name in ("PySide6", "fpdf2"):
        assert name in text, "в перечне нет компонента под LGPL: %s" % name


def test_lgpl_texts_attached():
    """AC 3: тексты лицензий, требующих приложения, лежат в поставке."""
    assert LICENSES_DIR.is_dir(), "нет папки licenses/ с текстами лицензий"
    for name in ("LGPL-3.0.txt", "GPL-3.0.txt"):
        path = LICENSES_DIR / name
        assert path.exists(), "нет текста лицензии licenses/%s" % name
        text = path.read_text(encoding="utf-8", errors="replace")
        assert len(text) > 5000, "текст лицензии %s подозрительно короткий" % name
        assert "GNU" in text, "файл %s не похож на текст лицензии GNU" % name


def test_installer_ships_license_files():
    """AC 4: файлы устанавливаются рядом с программой."""
    text = INSTALLER.read_text(encoding="utf-8")
    files_section = text.split("[Files]", 1)[1].split("[", 1)[0]
    assert "LICENSE" in files_section, "лицензия не попадает в поставку"
    assert "THIRD-PARTY-LICENSES.md" in files_section, "перечень чужих лицензий не попадает в поставку"
    assert "licenses" in files_section, "тексты лицензий не попадают в поставку"
    assert "LicenseFile=" in text, "лицензионные условия не показываются в мастере установки"


def _run():
    tests = [
        test_license_present,
        test_security_md_is_filled_in,
        test_third_party_list_present,
        test_lgpl_texts_attached,
        test_installer_ships_license_files,
    ]
    for t in tests:
        t()
        print("PASS: %s" % t.__name__)
    print("US-090 regression: ALL PASS")


if __name__ == "__main__":
    _run()
