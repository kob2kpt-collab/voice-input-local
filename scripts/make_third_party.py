# -*- coding: utf-8 -*-
"""Собрать перечень чужих компонентов и их лицензий (US-090, AC 3 и AC 5).

Перечень СОБИРАЕТСЯ из закреплённого состава (`requirements.lock.txt`), а не
пишется руками: список руками устаревает после первого же обновления
зависимости, и служба информационной безопасности получает документ, который
не соответствует поставке.

Отдельно проверяется, что для компонентов под LGPL приложен текст лицензии
(папка `licenses/`) — это обязательное условие их распространения.

Запуск:

    .venv\\Scripts\\python.exe scripts\\make_third_party.py

Код возврата: 0 — файл собран, 1 — не хватает текста лицензии.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from make_sbom import LOCKFILE, app_version, pypi_meta, read_lock  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
LICENSES_DIR = REPO_ROOT / "licenses"
OUTPUT = REPO_ROOT / "THIRD-PARTY-LICENSES.md"
def _force_utf8_output() -> None:
    """Печатать по-русски можно на любой консоли.

    Windows отдаёт скрипту кодировку консоли (cp1252/cp866), и обычный print с
    кириллицей роняет процесс UnicodeEncodeError. Из-за этого сборка 4.22.0
    упала на шаге тестов: все проверки прошли, а запускальщик умер на итоговой
    строке — и выпуск не опубликовался. Понижаем непечатаемые символы вместо
    падения; на UTF-8-консоли ничего не меняется.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


_force_utf8_output()

# Лицензии, требующие приложить полный текст, и файл с этим текстом.
TEXT_REQUIRED = {
    "LGPL": "LGPL-3.0.txt",
    "GPL": "GPL-3.0.txt",
}

HEADER = """# Компоненты сторонних разработчиков

Voice Input Local использует библиотеки с открытым исходным кодом. Ниже — их
перечень с версиями и лицензиями.

**Файл создаётся автоматически, руками не редактируется.** Источник — закреплённый
состав сборки `requirements.lock.txt` (US-089), поэтому перечень всегда совпадает
с тем, из чего собран выпуск. Пересобрать:

```
.venv\\Scripts\\python.exe scripts\\make_third_party.py
```

Полные тексты лицензий, требующих приложения (LGPL, GPL), лежат в папке
`licenses/` и входят в поставку.

Собрано: {today} · версия программы: {version} · компонентов: {count}
"""

FOOTER = """
## Отдельные замечания

* **PySide6, PySide6_Essentials, PySide6_Addons, shiboken6** (Qt for Python)
  распространяются под LGPL-3.0. Условие LGPL — возможность заменить библиотеку
  на другую версию — соблюдается: программа собирается в режиме одной папки, и
  библиотеки Qt лежат отдельными DLL рядом с исполняемым файлом, а не вшиты в
  него. Полные тексты — `licenses/LGPL-3.0.txt` и `licenses/GPL-3.0.txt`
  (LGPL-3.0 действует как дополнение к GPL-3.0 и без него не читается).
* **fpdf2** распространяется под LGPL-3.0 на тех же условиях.
* **pyinstaller** используется как средство сборки. Его лицензия GPL-2.0
  содержит исключение, разрешающее распространять собранные им программы под
  любой лицензией; сам PyInstaller в поставку не входит — в неё попадает только
  его загрузчик, на который это исключение и распространяется.
* Модели распознавания речи (Whisper, Parakeet) в поставку **не входят**: они
  скачиваются отдельно или раздаются из корпоративной папки, и их лицензии
  здесь не перечислены.
"""


def build(offline: bool = False) -> tuple[str, list[str]]:
    packages = read_lock(LOCKFILE)
    if not packages:
        raise SystemExit("Замок пуст или не прочитан: %s" % LOCKFILE)

    rows = []
    by_license: dict[str, list[str]] = defaultdict(list)
    for package in packages:
        name, version = package["name"], package["version"]
        meta = {} if offline else pypi_meta(name, version)
        license_name = meta.get("license") or "не определена"
        homepage = meta.get("homepage") or "https://pypi.org/project/%s/" % name
        rows.append((name, version, license_name, homepage))
        by_license[license_name].append(name)

    lines = [HEADER.format(today=date.today().isoformat(), version=app_version(), count=len(rows))]
    lines.append("\n## Перечень\n")
    lines.append("| Компонент | Версия | Лицензия | Адрес |")
    lines.append("|---|---|---|---|")
    for name, version, license_name, homepage in sorted(rows, key=lambda r: r[0].lower()):
        lines.append("| %s | %s | %s | %s |" % (name, version, license_name, homepage))

    lines.append("\n## Сводка по лицензиям\n")
    lines.append("| Лицензия | Компонентов |")
    lines.append("|---|---|")
    for license_name, names in sorted(by_license.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        lines.append("| %s | %d |" % (license_name, len(names)))

    # Проверяем, что тексты лицензий, требующих приложения, на месте.
    problems: list[str] = []
    for license_name, names in by_license.items():
        upper = license_name.upper()
        for marker, filename in TEXT_REQUIRED.items():
            if marker in upper and not (LICENSES_DIR / filename).exists():
                problems.append(
                    "нет файла licenses/%s, а он требуется для: %s" % (filename, ", ".join(sorted(names)))
                )
    lines.append(FOOTER)
    return "\n".join(lines), sorted(set(problems))


def main() -> int:
    parser = argparse.ArgumentParser(description="Собрать THIRD-PARTY-LICENSES.md")
    parser.add_argument("--output", default=str(OUTPUT))
    parser.add_argument("--offline", action="store_true", help="не обращаться к PyPI за лицензиями")
    args = parser.parse_args()

    content, problems = build(offline=args.offline)
    Path(args.output).write_text(content, encoding="utf-8")
    print("Записан %s" % args.output)
    if problems:
        print("\nНЕ ХВАТАЕТ ТЕКСТОВ ЛИЦЕНЗИЙ:")
        for item in problems:
            print("  - %s" % item)
        return 1
    print("Тексты лицензий, требующих приложения, на месте.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
