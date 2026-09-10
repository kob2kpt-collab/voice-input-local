# -*- coding: utf-8 -*-
"""Собрать перечень состава сборки — SBOM (US-089 AC 5, US-090 AC 5).

Формат — CycloneDX 1.5 JSON: его читают все распространённые средства анализа
состава, и служба информационной безопасности получает машинно-проверяемый
перечень, а не список в письме.

Источник — `requirements.lock.txt`, а НЕ окружение разработчика: перечень
обязан описывать то, из чего собран выпуск. Отсюда же берутся лицензии
компонентов для NOTICE (US-090): один источник фактов на оба документа.

Скрипт ничего не устанавливает. Лицензии и авторы запрашиваются у PyPI; без
сети перечень всё равно собирается, но поля лицензий останутся пустыми — об
этом печатается предупреждение, чтобы такой файл не ушёл в выпуск незаметно.

Запуск:

    .venv\\Scripts\\python.exe scripts\\make_sbom.py --output sbom.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCKFILE = REPO_ROOT / "requirements.lock.txt"
PYPI_JSON = "https://pypi.org/pypi/{name}/{version}/json"

PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\]+)")
HASH_RE = re.compile(r"--hash=sha256:([0-9a-f]{64})")


def read_lock(path: Path) -> list[dict]:
    """Разобрать замок: имя, версия, хэши каждого пакета."""
    packages: list[dict] = []
    current: dict | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = PIN_RE.match(line)
        if match:
            current = {"name": match.group(1), "version": match.group(2), "hashes": []}
            packages.append(current)
        if current is not None:
            current["hashes"].extend(HASH_RE.findall(line))
    return packages


def pypi_meta(name: str, version: str) -> dict:
    """Лицензия, описание и домашняя страница пакета. {} — узнать не удалось."""
    try:
        with urllib.request.urlopen(PYPI_JSON.format(name=name, version=version), timeout=30) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return {}
    info = data.get("info") or {}
    # Порядок важен. Свежие пакеты объявляют лицензию выражением по PEP 639
    # (license_expression), поле license у них пустое; у старых наоборот, а
    # часть из них кладёт в license ВЕСЬ текст лицензии — тогда короткое имя
    # остаётся только в классификаторах.
    license_name = (info.get("license_expression") or "").strip()
    if not license_name:
        license_name = (info.get("license") or "").strip()
    if not license_name or len(license_name) > 120:
        for classifier in info.get("classifiers") or []:
            if classifier.startswith("License :: "):
                license_name = classifier.rsplit("::", 1)[-1].strip()
                break
    return {
        "license": license_name,
        "summary": (info.get("summary") or "").strip(),
        "homepage": (info.get("home_page") or info.get("project_url") or "").strip(),
        "author": (info.get("author") or "").strip(),
    }


def build_sbom(app_version: str, offline: bool = False) -> dict:
    packages = read_lock(LOCKFILE)
    if not packages:
        raise SystemExit("Замок пуст или не прочитан: %s" % LOCKFILE)

    components = []
    unknown_license = []
    for package in packages:
        name, version = package["name"], package["version"]
        meta = {} if offline else pypi_meta(name, version)
        license_name = meta.get("license") or ""
        if not license_name:
            unknown_license.append("%s==%s" % (name, version))
        component = {
            "type": "library",
            "bom-ref": "pkg:pypi/%s@%s" % (name.lower(), version),
            "name": name,
            "version": version,
            "purl": "pkg:pypi/%s@%s" % (name.lower(), version),
            "hashes": [{"alg": "SHA-256", "content": h} for h in package["hashes"][:8]],
        }
        if meta.get("summary"):
            component["description"] = meta["summary"]
        if meta.get("author"):
            component["author"] = meta["author"]
        if license_name:
            component["licenses"] = [{"license": {"name": license_name}}]
        components.append(component)

    if unknown_license:
        print("ВНИМАНИЕ: лицензия не определена у %d компонентов: %s"
              % (len(unknown_license), ", ".join(unknown_license[:8])), file=sys.stderr)

    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tools": [{"vendor": "Voice Input Local", "name": "scripts/make_sbom.py", "version": "1.0"}],
            "component": {
                "type": "application",
                "bom-ref": "pkg:generic/voice-input-local@%s" % app_version,
                "name": "Voice Input Local",
                "version": app_version,
                "description": "Локальный голосовой ввод и расшифровка аудио для Windows",
            },
        },
        "components": components,
    }


def app_version() -> str:
    init = (REPO_ROOT / "voice_input_app" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', init)
    return match.group(1) if match else "0.0.0"


def main() -> int:
    parser = argparse.ArgumentParser(description="Собрать SBOM (CycloneDX JSON)")
    parser.add_argument("--output", default=str(REPO_ROOT / "sbom.json"))
    parser.add_argument("--offline", action="store_true", help="не обращаться к PyPI за лицензиями")
    args = parser.parse_args()

    sbom = build_sbom(app_version(), offline=args.offline)
    Path(args.output).write_text(json.dumps(sbom, indent=2, ensure_ascii=False), encoding="utf-8")
    print("SBOM записан: %s, компонентов %d" % (args.output, len(sbom["components"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
