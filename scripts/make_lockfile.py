# -*- coding: utf-8 -*-
"""Собрать закреплённый состав зависимостей (US-089).

Зачем. Служба информационной безопасности согласует программу по проверяемому
составу, а не по обещанию разработчика. `requirements.txt` задаёт диапазоны
(«PySide6>=6.7»), поэтому две сборки одного тега в разные дни могут собраться
из разных версий. Этот скрипт превращает диапазоны в точный список версий с
хэшами — `requirements.lock.txt`, — по которому и собирается релиз.

Как работает:

1. Версии, стоящие в окружении сборки, становятся ОГРАНИЧЕНИЯМИ: закреплять
   надо то, на чём программа проверялась, а не то, что вышло на PyPI сегодня
   утром.
2. `pip install --dry-run --report` разрешает ПОЛНОЕ дерево зависимостей
   requirements.txt с учётом этих ограничений, ничего не устанавливая. Отсюда
   берётся состав — так в замок не попадают ни остатки прежних версий из
   рабочего окружения разработчика (fastapi, keyboard и т. п.), ни пропуски:
   если из-за закреплённой версии нужна ещё одна зависимость, pip добавит её
   сам. Подставлять свои версии в готовый ответ нельзя — huggingface_hub 1.21
   требует typer, а 1.31 уже нет, и такой замок не установился бы целиком.
3. Хэши всех файлов каждой версии запрашиваются у PyPI (JSON API). Пин без
   хэша не защищает от подмены файла на зеркале, а pip в режиме
   --require-hashes требует хэши для ВСЕХ пакетов сразу.

Запуск (из корня репозитория):

    .venv\\Scripts\\python.exe scripts\\make_lockfile.py

Порядок обновления закреплённых версий описан в docs/enterprise-deploy.md.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = REPO_ROOT / "requirements.txt"
LOCKFILE = REPO_ROOT / "requirements.lock.txt"
PYPI_JSON = "https://pypi.org/pypi/{name}/{version}/json"
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


def resolve_tree(python: str, requirements: Path, constraints: dict[str, str] | None = None) -> list[dict]:
    """Разрешить дерево зависимостей, ничего не устанавливая.

    constraints — версии, которые нужно закрепить (проверенное окружение
    сборки). Передаются pip как файл ограничений, а НЕ подставляются в ответ
    задним числом: иначе более старая версия пакета потянула бы зависимость,
    которой в разрешении не было (huggingface_hub 1.21 требует typer, а 1.31
    уже нет), и замок оказался бы неполным — pip отказался бы ставить его
    целиком в режиме --require-hashes.
    """
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "resolve.json"
        cmd = [
            python, "-m", "pip", "install",
            "--dry-run", "--ignore-installed", "--quiet",
            "--report", str(report),
            "-r", str(requirements),
        ]
        if constraints:
            constraints_file = Path(tmp) / "constraints.txt"
            constraints_file.write_text(
                "\n".join("%s==%s" % (name, version) for name, version in sorted(constraints.items())),
                encoding="utf-8",
            )
            cmd += ["-c", str(constraints_file)]
        print("Разрешаю зависимости: %s" % " ".join(cmd[3:]))
        subprocess.run(cmd, check=True)
        data = json.loads(report.read_text(encoding="utf-8"))
    return list(data.get("install") or [])


def installed_versions(python: str) -> dict[str, str]:
    """Версии, реально стоящие в окружении сборки (на них программа проверена)."""
    out = subprocess.run(
        [python, "-m", "pip", "freeze", "--all"],
        check=True, capture_output=True, text=True,
    ).stdout
    versions: dict[str, str] = {}
    for line in out.splitlines():
        if "==" not in line or line.startswith("-"):
            continue
        name, _, version = line.partition("==")
        versions[canonical(name)] = version.strip()
    return versions


def canonical(name: str) -> str:
    """Каноническое имя пакета (PEP 503): регистр и -_. не различаются."""
    return "".join("-" if ch in "-_." else ch for ch in name.lower())


def pypi_hashes(name: str, version: str) -> list[str]:
    """sha256 всех файлов выпуска. Пустой список — версии нет на PyPI."""
    url = PYPI_JSON.format(name=name, version=version)
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 - PyPI
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return []
        raise
    hashes = []
    for item in data.get("urls") or []:
        digest = (item.get("digests") or {}).get("sha256")
        if digest and digest not in hashes:
            hashes.append(digest)
    return hashes


def build_lock(python: str) -> str:
    # Сначала — что стоит в окружении сборки: на этих версиях программа
    # проверялась, и закреплять надо именно их, а не то, что вышло на PyPI
    # сегодня утром. Затем pip разрешает дерево С УЧЁТОМ этих ограничений и
    # сам добавляет то, что из-за них понадобилось.
    local = installed_versions(python)
    tree = resolve_tree(python, REQUIREMENTS, constraints=local)

    rows: list[tuple[str, str, list[str]]] = []
    problems: list[str] = []
    for package in tree:
        meta = package.get("metadata") or {}
        name = str(meta.get("name") or "").strip()
        if not name:
            continue
        version = str(meta.get("version") or "").strip()
        hashes = pypi_hashes(name, version)
        if not hashes:
            # Пакет не с PyPI (локальное колесо, внутреннее зеркало): пин без
            # хэша оставить нельзя — pip откажется ставить файл целиком.
            problems.append("%s==%s: хэши на PyPI не найдены, пакет в замок НЕ попал" % (name, version))
            continue
        rows.append((name, version, hashes))

    rows.sort(key=lambda row: canonical(row[0]))

    lines = [
        "# Закреплённый состав зависимостей Voice Input Local (US-089).",
        "#",
        "# НЕ РЕДАКТИРОВАТЬ ВРУЧНУЮ. Файл создаётся скриптом:",
        "#     .venv\\Scripts\\python.exe scripts\\make_lockfile.py",
        "#",
        "# Точные версии + хэши: сборка одного и того же тега в разные дни даёт",
        "# один и тот же состав, а подменённый на зеркале файл не установится.",
        "# Диапазоны версий остаются в requirements.txt — он источник намерения,",
        "# этот файл источник факта. Порядок обновления — docs/enterprise-deploy.md.",
        "#",
        "# Создан: %s, пакетов: %d" % (date.today().isoformat(), len(rows)),
        "",
    ]
    for name, version, hashes in rows:
        first, rest = hashes[0], hashes[1:]
        line = "%s==%s --hash=sha256:%s" % (name, version, first)
        if rest:
            line += " \\"
        lines.append(line)
        for i, digest in enumerate(rest):
            tail = " \\" if i < len(rest) - 1 else ""
            lines.append("    --hash=sha256:%s%s" % (digest, tail))
    if problems:
        print("\nВНИМАНИЕ:")
        for item in problems:
            print("  - %s" % item)
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Собрать requirements.lock.txt")
    parser.add_argument("--python", default=sys.executable, help="интерпретатор окружения сборки")
    parser.add_argument("--check", action="store_true",
                        help="не записывать файл, а сверить его с текущим состоянием")
    args = parser.parse_args()

    content = build_lock(args.python)
    if args.check:
        if not LOCKFILE.exists():
            print("НЕТ ФАЙЛА: %s" % LOCKFILE)
            return 1
        same = LOCKFILE.read_text(encoding="utf-8") == content
        print("Замок актуален" if same else "Замок РАСХОДИТСЯ с requirements.txt/окружением")
        return 0 if same else 1
    LOCKFILE.write_text(content, encoding="utf-8")
    print("\nЗаписан %s" % LOCKFILE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
