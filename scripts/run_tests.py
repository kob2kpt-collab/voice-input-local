# -*- coding: utf-8 -*-
"""Прогнать все тесты проекта одной командой (US-089, AC 4).

В проекте нет pytest: тесты — самостоятельные скрипты, каждый со своим _run().
Так сложилось намеренно (не нужна лишняя зависимость в окружении сборки), но
для релизного сценария нужен один вход, который вернёт ненулевой код, если
хоть один файл упал, — иначе выпуск опубликуется поверх сломанных проверок.

Запуск:

    .venv\\Scripts\\python.exe scripts\\run_tests.py

Код возврата: 0 — всё прошло, 1 — есть падения.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"
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


def main() -> int:
    files = sorted(TESTS_DIR.glob("test_*.py"))
    if not files:
        print("Тестов не найдено в %s" % TESTS_DIR)
        return 1

    failed: list[tuple[str, str]] = []
    started = time.time()
    for path in files:
        name = path.name
        run = subprocess.run(
            [sys.executable, str(path)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        ok = run.returncode == 0
        print("%-46s %s" % (name, "OK" if ok else "ПАДЕНИЕ"), flush=True)
        if not ok:
            failed.append((name, (run.stdout or "") + (run.stderr or "")))

    print("\nФайлов: %d, упало: %d, за %.1f с" % (len(files), len(failed), time.time() - started))
    for name, output in failed:
        print("\n" + "=" * 70)
        print("ПАДЕНИЕ: %s" % name)
        print("=" * 70)
        print(output.strip()[-4000:])
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
