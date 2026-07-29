# -*- coding: utf-8 -*-
"""Регресс-тест: версия приложения одинакова во всех пользовательских местах."""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INIT_PATH = REPO_ROOT / "voice_input_app" / "__init__.py"
INSTALLER_PATH = REPO_ROOT / "installer" / "VoiceInputLocal.iss"
CHANGELOG_PATH = REPO_ROOT / "CHANGELOG.md"
README_PATH = REPO_ROOT / "README.md"
UI_PATH = REPO_ROOT / "voice_input_app" / "ui.py"


def _current_version() -> str:
    match = re.search(
        r'__version__\s*=\s*["\']([^"\']+)["\']',
        INIT_PATH.read_text(encoding="utf-8"),
    )
    assert match, "не найден __version__"
    version = match.group(1)
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), f"некорректная версия: {version}"
    return version


def test_version_is_consistent() -> None:
    version = _current_version()
    installer = INSTALLER_PATH.read_text(encoding="utf-8")
    changelog = CHANGELOG_PATH.read_text(encoding="utf-8")
    readme_head = "\n".join(README_PATH.read_text(encoding="utf-8").splitlines()[:8])

    assert f'#define MyAppVersion "{version}"' in installer
    assert f"## v{version}" in changelog
    assert f"Версия v{version}:" in readme_head


def test_ui_reads_the_single_version_source() -> None:
    ui = UI_PATH.read_text(encoding="utf-8")
    assert "from . import __version__" in ui
    assert 'self.setWindowTitle(f"Voice Input Local v{__version__}")' in ui
    assert 'Версия установленной программы: v{__version__}' in ui


def _run() -> None:
    tests = [test_version_is_consistent, test_ui_reads_the_single_version_source]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print("Version consistency regression: ALL PASS")


if __name__ == "__main__":
    _run()
