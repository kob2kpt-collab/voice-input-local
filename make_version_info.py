# -*- coding: utf-8 -*-
"""US-056: генерация ресурса версии Windows (version_info.txt) для PyInstaller.

Берёт версию из ЕДИНОГО источника — voice_input_app/__init__.py (__version__),
парсит X.Y.Z в 4-компонентный кортеж и пишет version_info.txt в формате
VSVersionInfo. Файл подключается к сборке через `--version-file`, чтобы у
собранного VoiceInputLocal.exe в свойствах файла стояла реальная версия, а не
0.0.0.0. Запускается из build_exe.bat перед PyInstaller (и в CI, т.к. CI зовёт
build_exe.bat). Зависимостей нет (только стандартная библиотека).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INIT_FILE = ROOT / "voice_input_app" / "__init__.py"
OUT_FILE = ROOT / "version_info.txt"


def read_version() -> str:
    src = INIT_FILE.read_text(encoding="utf-8")
    m = re.search(r"""__version__\s*=\s*["']([^"']+)["']""", src)
    if not m:
        raise SystemExit(f"Не найден __version__ в {INIT_FILE}")
    return m.group(1)


def version_tuple(version: str) -> tuple[int, int, int, int]:
    parts = [int(p) for p in re.findall(r"\d+", version)[:4]]
    parts += [0] * (4 - len(parts))
    return parts[0], parts[1], parts[2], parts[3]


def build_content(version: str) -> str:
    a, b, c, d = version_tuple(version)
    return f"""# UTF-8
# Автогенерация make_version_info.py (US-056). Не редактировать вручную.
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({a}, {b}, {c}, {d}),
    prodvers=({a}, {b}, {c}, {d}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [
          StringStruct('CompanyName', 'Voice Input Local'),
          StringStruct('FileDescription', 'Voice Input Local'),
          StringStruct('FileVersion', '{version}'),
          StringStruct('InternalName', 'VoiceInputLocal'),
          StringStruct('OriginalFilename', 'VoiceInputLocal.exe'),
          StringStruct('ProductName', 'Voice Input Local'),
          StringStruct('ProductVersion', '{version}')
        ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def main() -> None:
    version = read_version()
    OUT_FILE.write_text(build_content(version), encoding="utf-8")
    print(f"version_info.txt: {version} -> {version_tuple(version)}")


if __name__ == "__main__":
    main()
